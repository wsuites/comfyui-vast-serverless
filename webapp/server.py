#!/usr/bin/env python3
"""Local API for the web page, dispatching to workers we rent ourselves.

Runs on your machine, never on the worker. It holds the Vast API key
server-side so the browser never sees it, and streams progress to the page over
Server-Sent Events.

This process serves JSON and rendered files only. The page itself is the Astro
app in ``apps/web``, which proxies ``/api/**`` and ``/results/**`` here.

    python webapp/server.py            # the API; open the Astro port instead

Jobs no longer go through the Vast serverless endpoint. That router made one
call carry both the work and the reservation, so a request that died in flight
left its slot held by a worker doing nothing and every later request queued
behind it - measured on five distinct machines. ``scripts/fleet.py`` owns the
renting instead, and this process talks to ComfyUI's own HTTP API on the
instance, which is what makes a cancel a real ``/interrupt`` and a dead client
cost nothing.

About progress: ComfyUI reports per-node, not per-step, so what this serves is
*phase* progress, which is where the time actually goes - a cold start is
minutes and the render is seconds. Phases come from the dispatch path itself
plus polling the Vast API for the instance's real state.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from argparse import Namespace
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

import config
import fleet

from ab_modelo import ANIMA_UNET, ARMS
from ab_modelo import build as build_arm_workflow
from call_endpoint import RMBG_MODELS, build_workflow
from vast_state import describe, goes_backwards, instances, workers
from vast_state import reboot as _vast_reboot

OUT_DIR = ROOT / "output" / "web"
HISTORY = OUT_DIR / "index.jsonl"
POLL_SECONDS = 5
# How long a request waits for a model that is still downloading behind the
# ready signal. The deferred set is ~5.6 GB and lands in about a minute on a
# healthy origin; past this the file is not coming and the caller deserves the
# name of it rather than another minute of billing.
MODEL_WAIT = 300.0


def setting(name: str, default: str) -> str:
    """The environment first, then ``.env``, then the default.

    Launched by the CLI, this process inherits everything it needs because the
    CLI exports it. Launched by systemd it inherits almost nothing, and the
    only file that knows the deployment is ``.env`` - the same one
    ``scripts/config.py`` reads. Reading ``os.environ`` alone made
    ``WEB_RETENTION_GB`` a decoration in that case: documented, set, ignored.
    """
    v = os.environ.get(name) or config.ENV.get(name)
    return default if v is None or v == "" else v


# Cap on the rendered gallery on disk, oldest job dirs deleted first. The VPS
# has ~12 GB free and nothing else reclaims this, so it is set here rather than
# left to whoever remembers. 0 disables.
RETENTION_GB = float(setting("WEB_RETENTION_GB", "5"))


async def _vast_state() -> dict:
    """Both Vast views, fetched together, off the event loop.

    The phase rules live in ``scripts/vast_state.py`` so the CLI and this page
    cannot drift apart.
    """
    insts, works = await asyncio.gather(
        asyncio.to_thread(instances),
        asyncio.to_thread(workers),
    )
    return describe(insts, works)


# -------------------------------------------------------------------- jobs


@dataclass
class Job:
    id: str
    params: dict
    created: float = field(default_factory=time.time)
    state: str = "running"           # running | done | error | cancelled
    phase: str = "submitting"
    detail: str = "Handing the request to the endpoint"
    worker: dict | None = None
    images: list[str] = field(default_factory=list)
    error: str | None = None
    latency: float | None = None
    log: list[dict] = field(default_factory=list)
    # "scene" runs the full pipeline from workflows/wf.json; "arm" runs the
    # bare text2img graph of scripts/ab_modelo.py, the only one whose numbers
    # are comparable with the table in docs/levers-and-dead-ends.md
    kind: str = "scene"
    label: str | None = None         # arm name, or None for scene jobs
    pair: str | None = None          # id shared by the two sides of a compare
    side: str | None = None          # "A" | "B"
    _seq: int = 0
    # The asyncio task driving this job, so it can be cancelled from the API.
    # Never serialized: snapshot() is what the page and the history file see.
    # A compare pair shares one task - both arms run inside the same coroutine.
    task: Any = field(default=None, repr=False, compare=False)

    def emit(self, phase: str, detail: str, **extra: Any) -> None:
        """Record a phase change. Repeated identical lines are collapsed.

        Phases only ever move forward. Vast's ``actual_status`` flickers back to
        ``loading`` on a machine that is up and serving, which would otherwise
        walk the stepper backwards from "rendering" to "booting" mid-render.
        """
        if goes_backwards(self.phase, phase):
            return
        if self.phase in ("done", "error", "cancelled") and phase != self.phase:
            return                       # nothing happens after a job stops
        if self.log and self.log[-1]["phase"] == phase and self.log[-1]["detail"] == detail:
            return
        self.phase = phase
        self.detail = detail
        self._seq += 1
        self.log.append({
            "seq": self._seq, "t": time.time() - self.created,
            "phase": phase, "detail": detail, **extra,
        })

    def snapshot(self) -> dict:
        return {
            "id": self.id, "state": self.state, "phase": self.phase,
            "detail": self.detail, "worker": self.worker, "images": self.images,
            "error": self.error, "latency": self.latency,
            "elapsed": time.time() - self.created, "log": self.log,
            "params": self.params,
            "kind": self.kind, "label": self.label,
            "pair": self.pair, "side": self.side,
            "created": self.created,
        }


JOBS: dict[str, Job] = {}


def _remember(job: Job) -> None:
    """Append one finished job to the on-disk history.

    ``JOBS`` is in memory and dies with the server; this file is what the
    gallery reads on a fresh start. One JSON object per line, appended, never
    rewritten - a crash mid-write costs the last line and nothing else.
    """
    entry = {
        "id": job.id, "created": job.created, "state": job.state,
        "kind": job.kind, "label": job.label, "pair": job.pair, "side": job.side,
        "images": job.images, "latency": job.latency, "error": job.error,
        "elapsed": time.time() - job.created, "params": job.params,
    }
    try:
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except Exception as exc:                       # history is never fatal
        print(f"history not written: {exc}", file=sys.stderr)


def _history(limit: int = 200) -> list[dict]:
    """Newest first. Entries whose images are gone from disk are dropped."""
    if not HISTORY.is_file():
        return []
    rows: list[dict] = []
    for line in HISTORY.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue                               # truncated tail, skip it
    alive = []
    for r in rows:
        kept = [u for u in r.get("images", [])
                if not u.startswith("/results/")
                or (OUT_DIR / u[len("/results/"):]).is_file()]
        if kept or r.get("state") == "error":
            r["images"] = kept
            alive.append(r)
    return alive[::-1][:limit]


def _save_blobs(job: Job, blobs: list[tuple[str, bytes]]) -> list[str]:
    """Write what came straight off the worker.

    Nothing round-trips through R2 on this path: the bytes were fetched from
    ComfyUI's /view while we held the instance, so there is no presigned URL to
    expire and no upload to fail after a successful render.
    """
    dest = OUT_DIR / job.id
    dest.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for i, (name, blob) in enumerate(blobs):
        suffix = Path(name).suffix or ".png"
        path = dest / f"{i:02d}{suffix}"
        path.write_bytes(blob)
        saved.append(f"/results/{job.id}/{path.name}")
    _prune_output(keep=job.id)
    return saved


def _human(n: float) -> str:
    """Bytes in the largest unit that leaves a significant digit."""
    for unit, size in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= size:
            return f"{n / size:.2f} {unit}"
    return f"{int(n)} B"


def _prune_output(keep: str | None = None) -> int:
    """Drop the oldest renders until the gallery fits under its cap.

    This exists because the directory only ever grew. On a laptop that is a
    slow annoyance; on the VPS it is 12 GB of free disk and a server that stops
    serving when it runs out, so the bill for forgetting is the whole page.

    Deleting files is enough on its own: ``_history()`` already drops entries
    whose images are gone from disk, so the gallery heals on the next read and
    there is no second list to keep in step. ``keep`` is the job being written
    right now - it is the newest and would never be picked anyway, but a cap
    set absurdly low should degrade to "keeps one" rather than to deleting the
    render the caller is still waiting for.
    """
    cap = RETENTION_GB * 1024 ** 3
    if cap <= 0:
        return 0
    dirs = []
    total = 0
    for d in OUT_DIR.iterdir():
        if not d.is_dir():
            continue                         # index.jsonl lives alongside them
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        dirs.append((d.stat().st_mtime, d, size))
        total += size
    if total <= cap:
        return 0
    freed = 0
    for _, d, size in sorted(dirs):          # oldest first
        if total <= cap:
            break
        if keep is not None and d.name == keep:
            continue
        try:
            for f in sorted(d.rglob("*"), reverse=True):
                f.unlink() if f.is_file() else f.rmdir()
            d.rmdir()
        except OSError as exc:               # a locked file is not fatal
            print(f"retention: {d.name}: {exc}", file=sys.stderr)
            continue
        total -= size
        freed += size
    if freed:
        # Report in the unit the number actually has. "freed 0.00 GB" is the
        # line an operator reads when wondering whether retention did
        # anything, and a 0 that means "yes, 40 MB" answers the wrong way.
        print(f"retention: freed {_human(freed)} "
              f"under a {_human(cap)} cap", file=sys.stderr)
    return freed


async def _watch_infra(job: Job) -> None:
    """Report what the worker is doing until the request comes back."""
    while True:
        info = await _vast_state()
        job.worker = info["worker"]
        # Once the reply is in, the instance list has nothing left to add, and
        # overwriting 'saving'/'done' would make the UI walk backwards.
        if job.phase not in ("saving", "done", "error"):
            job.emit(info["phase"], info["detail"])
        await asyncio.sleep(POLL_SECONDS)


def _build_arm(job: Job) -> dict:
    """Bare text2img graph for one arm, with the page's overrides applied.

    ``ab_modelo.build`` reads steps and cfg from its own ARMS table, which is
    what makes its numbers comparable with the doc. Overriding them here is a
    deliberate opt-in: the graph is patched after the fact instead of mutating
    the shared table, so a second arm running in parallel sees its own config.
    """
    p = job.params
    seed = p.get("seed")
    if seed is None:
        seed = int(uuid.uuid4().int % (2 ** 53))
        p["seed"] = seed                           # the page must see it
    ns = Namespace(
        prompt=p.get("prompt"), negative=p.get("negative"),
        width=p.get("width", 1024), height=p.get("height", 1024),
        anima_model=p.get("anima_model") or ANIMA_UNET,
    )
    wf = build_arm_workflow(job.label, seed, ns)

    steps, cfg = p.get("steps"), p.get("cfg")
    sampler = wf["22"]["inputs"]
    if cfg is not None:
        sampler["cfg"] = cfg
    if steps is not None:
        # beta57 keeps the step count in its scheduler node, not the sampler
        if "21" in wf and wf["21"]["class_type"] == "BetaSamplingScheduler":
            wf["21"]["inputs"]["steps"] = steps
        else:
            sampler["steps"] = steps
    return wf


async def _run_job(job: Job) -> None:
    args = Namespace(
        workflow=str(ROOT / "workflows" / "wf.json"),
        **{k: v for k, v in job.params.items()
           if k not in ("cost", "timeout", "arm")},
    )
    watcher = asyncio.create_task(_watch_infra(job))
    raw: Any = None
    url: str | None = None
    try:
        if job.kind == "arm":
            workflow = await asyncio.to_thread(_build_arm, job)
        else:
            workflow = await asyncio.to_thread(build_workflow, args)

        # Demand and supply used to travel on the same call: a request to the
        # serverless endpoint was both the job and the reservation for a slot,
        # so a request that died in flight left the slot wedged and every later
        # one queued behind a worker that was doing nothing. Renting is ours
        # now. ``fleet.up`` covers the whole decision table the page can hit -
        # an instance already serving, one merely stopped, none at all, and a
        # machine that refuses the booking - and only returns once we have
        # personally fetched /object_info off it, so a wedged host is rejected
        # at rent time instead of ten minutes into a timeout.
        job.emit("renting", "Bringing up a worker")
        # boot_cap is left at fleet's own (FLEET_BOOT_CAP, the ten-minute cold
        # start goal). The job's ``timeout`` is a render budget and is orders
        # of magnitude smaller; passing it here would abandon every cold start.
        inst = await asyncio.to_thread(fleet.up)
        if not inst:
            raise RuntimeError("Could not bring a worker up: no offer under "
                               f"{fleet.DPH_CEILING:.3f}/h passed the probe")
        # ``up`` hands back fleet's own state record, not a Vast instance dict:
        # it has already resolved and probed the URL, and re-deriving it here
        # would be a second, less informed guess at the same thing.
        url = inst.get("url")
        if not url:
            raise RuntimeError(f"Worker {inst.get('instance')} is up but its "
                               "ComfyUI port is not published")

        # A worker can come ready with the deferred half of its models still
        # landing: provisioning signals ready on the blocking set and keeps
        # downloading the rest behind it. Submitting into that window is what
        # produced a rented RTX 3090 answering "HTTP Error 400: Bad Request" to
        # every anima request on 2026-09-23 - ComfyUI was right, the VAE really
        # was not there. Wait for the files this workflow names, on the phase
        # the page is already showing, and name them if they never arrive.
        deadline = time.time() + MODEL_WAIT
        while True:
            # Held through the wait as well as the render. A worker that comes
            # up missing a model can sit here for MODEL_WAIT seconds without a
            # single job to touch(), and it is the daemon's own tick that would
            # collect it - the two only run side by side now that both live on
            # the server.
            await asyncio.to_thread(fleet.lease, inst.get("instance") or "")
            missing = await asyncio.to_thread(fleet.missing_inputs, url, workflow)
            if not missing:
                break
            if time.time() >= deadline:
                raise RuntimeError(
                    f"Worker {inst.get('instance')} is missing "
                    + ", ".join(missing)
                    + f" after {MODEL_WAIT:.0f}s")
            job.emit("renting", "Worker up - still downloading "
                                + ", ".join(m.split("=")[-1] for m in missing))
            await asyncio.sleep(5)

        # "generating", not "submitting": the stepper only moves forward, so
        # emitting an earlier phase here is silently dropped and the page sits
        # on "renting GPU" for the whole render.
        job.emit("generating", f"Rendering on {inst.get('gpu')} "
                               f"(instance {inst.get('instance')})")
        started = time.time()
        # ``fleet.tick()``'s idle release reads ``last_job`` from the state
        # file, and a page render is the only kind of use that never went
        # through ``fleet.render()``. Without these two calls the daemon
        # measures idle from ``ready_at`` and destroys a worker that is
        # serving the page - mid-render, ten minutes after it came ready.
        # Once on either side of the wait: the first claims the worker before
        # a long batch starts, the second resets the clock once it is served.
        await asyncio.to_thread(fleet.touch)
        prompt_id = await asyncio.to_thread(fleet.submit, url, workflow, "web")
        raw = await asyncio.to_thread(
            fleet.wait_job, url, prompt_id, float(job.params["timeout"]),
        )
        await asyncio.to_thread(fleet.touch)
        job.latency = time.time() - started

        blobs = await asyncio.to_thread(fleet.images, url, raw)
        if not blobs:
            raise RuntimeError(
                "The worker finished but produced no image. History entry "
                f"saved to output/web/{job.id}/raw.json"
            )

        job.emit("saving", f"Writing {len(blobs)} image(s)")
        job.images = _save_blobs(job, blobs)
        job.state = "done"
        job.emit("done", f"Finished in {job.latency:.1f}s")
    except asyncio.CancelledError:
        # Dispatching straight at ComfyUI means a cancel is a real cancel: the
        # sampler has an /interrupt route, and nothing is holding a reservation
        # that has to be abandoned. The GPU minute is genuinely reclaimed, so
        # do not repeat the old message about the worker finishing anyway.
        job.state = "cancelled"
        if url:
            with suppress(Exception):
                await asyncio.shield(asyncio.to_thread(fleet.interrupt, url))
        # Log against the phase it died in rather than inventing a new one: the
        # stepper should keep showing how far this render got.
        job.emit(job.phase, "Cancelled - the worker was interrupted")
        raise
    except Exception as exc:
        job.state = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        job.emit("error", job.error)
    except BaseException as exc:                    # noqa: BLE001
        # A job must not be able to end the process. `SystemExit` was the one
        # that got through: raised deep in a config lookup, carried out of the
        # worker thread by ``to_thread``, past the handler above because it is
        # not an ``Exception``, and out through uvicorn. The server died, the
        # job table died with it, and the page - whose stream had just closed -
        # sat on "Bringing up a worker" with a stopped clock. A crash that
        # renders as patience is the expensive kind.
        job.state = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        job.emit("error", job.error)
        if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
            raise
    finally:
        watcher.cancel()
        try:
            (OUT_DIR / job.id).mkdir(parents=True, exist_ok=True)
            (OUT_DIR / job.id / "raw.json").write_text(
                json.dumps(raw, indent=2, default=str), encoding="utf-8",
            )
        except Exception:
            pass
        # The instance is deliberately left running. It is ours for the hour we
        # already bought, and the next request finds it warm instead of paying
        # a cold start again; ``scripts/fleet.py down`` releases it.
        _remember(job)


# --------------------------------------------------------------------- api


class JobRequest(BaseModel):
    prompt: str = Field(min_length=1)
    negative: str | None = None
    seed: int | None = None
    width: int = 1024
    height: int = 1024
    batch: int = 1
    steps: int | None = None
    cfg: float | None = None
    # The scene graph is SDXL-wired; "anima" swaps its loading subgraph for
    # the three-loader set. The LoRA and the face pass are levers of the SDXL
    # side: the LoRA file does not apply to a DiT, and the face pass is the
    # part of the pipeline least proven against one.
    family: str = "wai"
    anima_model: str | None = None
    lora: float | None = None
    no_face: bool = False
    # Cap on how many detections the face pass repaints, largest first. Left
    # unset it takes call_endpoint's default; 0 lifts it, which means whatever
    # YOLO returns - up to 300 sampling passes inside one request timeout.
    face_cap: int | None = None
    # The face pass repaints inside the face mask with its own prompt, which
    # beats the main one there; wf.json ships it with "red eyes" baked in.
    detail_prompt: str | None = None
    detail_negative: str | None = None
    no_upscale: bool = False
    remove_bg: str | None = None
    bg_refine: bool = False
    bg_sensitivity: float = 1.0
    bg_blur: int = 0
    bg_offset: int = 0
    cost: int = 100
    timeout: float = 900.0


class CompareRequest(BaseModel):
    """One prompt, two arms. The arms come from ``scripts/ab_modelo.py``."""
    prompt: str = Field(min_length=1)
    negative: str | None = None
    arm_a: str
    arm_b: str
    seed: int | None = None          # None = random, shared by both sides
    width: int = 1024
    height: int = 1024
    steps: int | None = None         # None = each arm's own recommended value
    cfg: float | None = None
    anima_model: str | None = None
    cost: int = 100
    timeout: float = 900.0


app = FastAPI(title="autoscaler-vast console")


@app.post("/api/jobs")
async def create_job(req: JobRequest) -> dict:
    if req.family not in ("wai", "anima"):
        raise HTTPException(400, f"family must be wai or anima, not {req.family!r}")
    if req.remove_bg and req.remove_bg not in RMBG_MODELS:
        raise HTTPException(400, f"remove_bg must be one of {sorted(RMBG_MODELS)}")
    job = Job(id=uuid.uuid4().hex[:12], params=req.model_dump())
    JOBS[job.id] = job
    job.emit("submitting", "Request queued for the endpoint")
    job.task = asyncio.create_task(_run_job(job))
    return {"job_id": job.id}


def _detail_defaults() -> dict:
    """The face pass prompts as they stand in wf.json.

    Read from the file rather than copied into the page: they are what the
    request actually uses when the form leaves the fields blank, and the
    shipped positive contains "red eyes", which is worth showing.
    """
    try:
        wf = json.loads((ROOT / "workflows" / "wf.json").read_text("utf-8"))
        return {
            "prompt": wf["42"]["inputs"]["value"],
            "negative": wf["55"]["inputs"]["value"],
        }
    except (OSError, KeyError, ValueError):
        return {"prompt": "", "negative": ""}


@app.get("/api/options")
async def list_options() -> dict:
    """Catalogues the page builds its selects from.

    Served from Python on purpose: hard-coding the arm names or the background
    models in the HTML is how the page and the scripts drift apart.
    """
    return {
        "arms": ARMS,
        "anima_model": ANIMA_UNET,
        "remove_bg": sorted(RMBG_MODELS),
        "detail": _detail_defaults(),
    }


@app.get("/api/history")
async def history(limit: int = 200) -> dict:
    return {"items": _history(limit)}


@app.post("/api/compare")
async def create_compare(req: CompareRequest) -> dict:
    for name in (req.arm_a, req.arm_b):
        if name not in ARMS:
            raise HTTPException(400, f"unknown arm {name!r}; have {sorted(ARMS)}")

    # One seed for both sides, fixed here rather than per job: a comparison
    # against two different seeds measures noise, not the model.
    seed = req.seed
    if seed is None:
        seed = int(uuid.uuid4().int % (2 ** 53))

    pair = uuid.uuid4().hex[:8]
    base = req.model_dump(exclude={"arm_a", "arm_b"}) | {"seed": seed}
    sides = []
    for side, arm in (("A", req.arm_a), ("B", req.arm_b)):
        job = Job(id=uuid.uuid4().hex[:12], params=dict(base, arm=arm),
                  kind="arm", label=arm, pair=pair, side=side)
        JOBS[job.id] = job
        sides.append(job)

    sides[0].emit("submitting", f"Request queued for the endpoint - arm {req.arm_a}")
    # The second arm cannot start now: one worker, one GPU, and across
    # families ComfyUI reloads ~5 GB of model between the two requests.
    sides[1].emit("submitting", f"Waiting for arm {req.arm_a} to finish")

    async def both() -> None:
        await _run_job(sides[0])
        sides[1].emit("submitting", f"Request queued for the endpoint - arm {req.arm_b}")
        await _run_job(sides[1])

    # Both arms hang off one task, so cancelling either stops the pair - which
    # is the honest behaviour: half a comparison measures nothing.
    task = asyncio.create_task(both())
    for j in sides:
        j.task = task
    return {"pair_id": pair, "jobs": [j.id for j in sides],
            "seed": seed, "arms": [req.arm_a, req.arm_b]}


@app.get("/api/jobs")
async def list_jobs() -> dict:
    ordered = sorted(JOBS.values(), key=lambda j: j.created, reverse=True)
    return {"jobs": [j.snapshot() for j in ordered[:50]]}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict:
    """Stop waiting for a running job.

    This buys back GPU time, which it did not use to. While jobs went through
    the serverless wrapper there was no interrupt route, so cancelling only
    stopped us waiting and the worker finished the render anyway. Dispatching
    at ComfyUI directly means the cancel reaches ``/interrupt`` and the sampler
    stops. The instance is deliberately left running - we already bought the
    hour, and the next request finds it warm.
    """
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    if job.state != "running":
        raise HTTPException(409, f"job is already {job.state}")
    if job.task is None or job.task.done():
        # Should not happen; if it does, the page must not keep showing a
        # spinner for a job nothing is driving.
        job.state = "cancelled"
        job.emit(job.phase, "Cancelled - nothing was driving this job")
        return job.snapshot()

    job.task.cancel()
    # Give the coroutine the one loop pass it needs to run its except/finally
    # and settle its own state, so the reply already carries the new one.
    with suppress(Exception, asyncio.CancelledError):
        await asyncio.wait({job.task}, timeout=2)

    # A compare pair shares the task: the arm that had not started yet is
    # cancelled too, and it never entered _run_job to say so itself.
    for other in JOBS.values():
        if other.task is job.task and other.state == "running":
            other.state = "cancelled"
            other.emit(other.phase, "Cancelled with the other arm")
    return job.snapshot()


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    if job_id not in JOBS:
        raise HTTPException(404, "unknown job")
    return JOBS[job_id].snapshot()


@app.get("/api/jobs/{job_id}/stream")
async def stream_job(job_id: str) -> StreamingResponse:
    if job_id not in JOBS:
        raise HTTPException(404, "unknown job")
    job = JOBS[job_id]

    async def events():
        # One frame per second unconditionally: the page needs the elapsed
        # clock to tick even while the phase sits still for minutes.
        while True:
            yield f"data: {json.dumps(job.snapshot())}\n\n"
            if job.state in ("done", "error", "cancelled"):
                break
            await asyncio.sleep(1)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/worker/reboot")
async def reboot_worker() -> dict:
    """Restart the worker container. The console's way out of a wedged slot.

    Refuses while the card is demonstrably busy: a real render is worth more
    than the convenience of not having to cancel it first. A *wedged* slot is
    not busy - that is the whole point - so the stall detector is what decides,
    not the raw request count.
    """
    info = await _vast_state()
    work = info.get("worker") or {}
    if work.get("reqs") and not work.get("stalled"):
        raise HTTPException(409, "The worker is rendering. Cancel the job first.")

    report = await asyncio.to_thread(_vast_reboot)
    if not report.get("ok"):
        raise HTTPException(502, report.get("detail") or "reboot refused by Vast")

    # Whatever was queued is now queued against a machine that is going away.
    for job in JOBS.values():
        if job.state == "running":
            job.emit(job.phase, "Worker rebooted from the console - this "
                                "request will not come back")
    return report


@app.get("/api/status")
async def status() -> dict:
    """Live worker + cost, independent of any job."""
    info = await _vast_state()
    if info["worker"] and info["worker"].get("start"):
        hours = max(0.0, (time.time() - float(info["worker"]["start"])) / 3600)
        info["worker"]["hours"] = hours
        info["worker"]["spent"] = hours * float(info["worker"].get("dph") or 0)
    return info


@app.get("/results/{job_id}/{name}")
async def result_file(job_id: str, name: str) -> FileResponse:
    path = (OUT_DIR / job_id / name).resolve()
    if not path.is_file() or OUT_DIR.resolve() not in path.parents:
        raise HTTPException(404, "no such result")
    return FileResponse(path)


if __name__ == "__main__":
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Authentication lives in the Astro app (apps/web/src/middleware.ts), which
    # is the only thing that asks for credentials. This process has none, and
    # it holds the Vast API key: binding it to anything but a loopback address
    # would hand that key to whoever can reach the port, tunnel included. Hence
    # API_HOST is read but a non-loopback value is refused.
    # `API_ORIGIN` names the deployment that owns the fleet. When it is set,
    # this machine is a client of that API, and a second process holding the
    # same Vast key would rent against the same account from a second door.
    origin = setting("API_ORIGIN", "")
    if origin:
        sys.exit(
            f"API_ORIGIN={origin} - the fleet lives there, so this API would be a "
            "second spender on the same Vast account. Clear API_ORIGIN in .env to "
            "run the all-in-one local stack."
        )

    host = setting("API_HOST", "127.0.0.1")
    port = int(setting("API_PORT", "8800"))
    if host not in ("127.0.0.1", "::1", "localhost"):
        sys.exit(f"API_HOST={host} is not a loopback address; refusing to bind.")
    print(f"autoscaler-vast api -> http://{host}:{port}", file=sys.stderr)
    uvicorn.run(app, host=host, port=port, log_level="warning")
