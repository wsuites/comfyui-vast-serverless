#!/usr/bin/env python3
"""Local API for the "mizuki" serverless endpoint.

Runs on your machine, never on the worker. It holds the Vast API key
server-side so the browser never sees it, submits jobs through the same
``/generate/sync`` route the CLI scripts use, and streams progress to the page
over Server-Sent Events.

This process serves JSON and rendered files only. The page itself is the Astro
app in ``apps/web``, which proxies ``/api/**`` and ``/results/**`` here.

    python webapp/server.py            # the API; open the Astro port instead

About progress: the pyworker only exposes ``/generate/sync`` and ``/health``
(checked on a live instance, 2026-09-20). There is no per-step callback and
ComfyUI's own port is not published, so a percent-complete bar is impossible
without changing the template. What this serves instead is *phase* progress,
which is where the time actually goes: a cold start is ~7 min and the render
is ~18 s. Phases come from polling the Vast API for the worker's real state.
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

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from vastai import Serverless

from ab_modelo import ANIMA_UNET, ARMS
from ab_modelo import build as build_arm_workflow
from call_endpoint import RMBG_MODELS, build_workflow, resolve_api_key
from config import ENDPOINT_NAME
from vast_state import (describe, extract_image_urls, goes_backwards, instances,
                        unstick, workers)
from vast_state import reboot as _vast_reboot

OUT_DIR = ROOT / "output" / "web"
HISTORY = OUT_DIR / "index.jsonl"
POLL_SECONDS = 5


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


async def _save_locally(job: Job, urls: list[str]) -> list[str]:
    """Mirror results to disk. The R2 links are presigned and expire in 7 days."""
    saved: list[str] = []
    dest = OUT_DIR / job.id
    dest.mkdir(parents=True, exist_ok=True)
    async with aiohttp.ClientSession() as session:
        for i, url in enumerate(urls):
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                    resp.raise_for_status()
                    blob = await resp.read()
            except Exception as exc:                      # keep the remote URL
                job.emit("saving", f"Could not mirror image {i + 1}: {exc}")
                continue
            path = dest / f"{i:02d}.png"
            path.write_bytes(blob)
            saved.append(f"/results/{job.id}/{path.name}")
    return saved


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
    # Before anything else: a worker stranded on a full machine cannot be
    # rescued by waiting, and the page would show "queued" for the whole
    # timeout. Replacing it here turns a dead job into a cold start.
    fix = await asyncio.to_thread(unstick)
    if fix.get("acted"):
        job.emit("renting", f"Replaced a stranded worker ({fix['reason']}) - "
                            "renting another machine")

    watcher = asyncio.create_task(_watch_infra(job))
    client = Serverless(api_key=resolve_api_key())
    raw: Any = None
    try:
        if job.kind == "arm":
            workflow = await asyncio.to_thread(_build_arm, job)
        else:
            workflow = await asyncio.to_thread(build_workflow, args)
        payload = {"input": {"request_id": str(uuid.uuid4()), "workflow_json": workflow}}

        endpoint = await client.get_endpoint(name=ENDPOINT_NAME)
        started = time.time()
        raw = await endpoint.request(
            "/generate/sync", payload,
            cost=job.params["cost"], timeout=job.params["timeout"],
        )
        job.latency = time.time() - started

        urls = extract_image_urls(raw)
        if not urls:
            raise RuntimeError(
                "The worker replied but no image URL was found. Raw reply saved "
                f"to output/web/{job.id}/raw.json"
            )

        job.emit("saving", f"Downloading {len(urls)} image(s) from R2")
        job.images = await _save_locally(job, urls) or urls
        job.state = "done"
        job.emit("done", f"Finished in {job.latency:.1f}s")
    except asyncio.CancelledError:
        # Cancelling stops *this* side of the wire. The request is already at
        # the endpoint and ComfyUI has no interrupt route through the api
        # wrapper, so the worker finishes the render and throws the pixels
        # away; what is reclaimed is the slot in front of the operator, not
        # the GPU minute. Say so rather than implying the machine stopped.
        job.state = "cancelled"
        # Log against the phase it died in rather than inventing a new one: the
        # stepper should keep showing how far this render got.
        job.emit(job.phase, "Cancelled - the worker may still be finishing "
                            "this render, its result is discarded")
        raise
    except Exception as exc:
        job.state = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        job.emit("error", job.error)
    finally:
        watcher.cancel()
        try:
            (OUT_DIR / job.id).mkdir(parents=True, exist_ok=True)
            (OUT_DIR / job.id / "raw.json").write_text(
                json.dumps(raw, indent=2, default=str), encoding="utf-8",
            )
        except Exception:
            pass
        # shield + suppress: this runs while a CancelledError is propagating,
        # and closing the session must not be what leaks the aiohttp connector.
        with suppress(Exception, asyncio.CancelledError):
            await asyncio.shield(client.close())
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


app = FastAPI(title="mizuki console")


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

    What this does and does not do: it cancels the coroutine awaiting
    ``/generate/sync``, which frees the console and releases the compare chain.
    It does not interrupt ComfyUI - the api wrapper exposes no route for that,
    and the worker will finish the render it already started. So this buys back
    attention, not GPU time.
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
    host = os.environ.get("API_HOST", "127.0.0.1")
    port = int(os.environ.get("API_PORT", "8800"))
    if host not in ("127.0.0.1", "::1", "localhost"):
        sys.exit(f"API_HOST={host} is not a loopback address; refusing to bind.")
    print(f"mizuki api -> http://{host}:{port}", file=sys.stderr)
    uvicorn.run(app, host=host, port=port, log_level="warning")
