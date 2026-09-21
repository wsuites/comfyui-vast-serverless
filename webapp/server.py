#!/usr/bin/env python3
"""Local web UI for the "mizuki" serverless endpoint.

Runs on your machine, never on the worker. It holds the Vast API key
server-side so the browser never sees it, submits jobs through the same
``/generate/sync`` route the CLI scripts use, and streams progress to the page
over Server-Sent Events.

    python webapp/server.py            # then open http://127.0.0.1:8800

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
import sys
import time
import uuid
from argparse import Namespace
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from vastai import Serverless

from call_endpoint import RMBG_MODELS, build_workflow, resolve_api_key
from config import ENDPOINT_NAME
from vast_state import (describe, extract_image_urls, goes_backwards, instances,
                        unstick, workers)

OUT_DIR = ROOT / "output" / "web"
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
    state: str = "running"           # running | done | error
    phase: str = "submitting"
    detail: str = "Handing the request to the endpoint"
    worker: dict | None = None
    images: list[str] = field(default_factory=list)
    error: str | None = None
    latency: float | None = None
    log: list[dict] = field(default_factory=list)
    _seq: int = 0

    def emit(self, phase: str, detail: str, **extra: Any) -> None:
        """Record a phase change. Repeated identical lines are collapsed.

        Phases only ever move forward. Vast's ``actual_status`` flickers back to
        ``loading`` on a machine that is up and serving, which would otherwise
        walk the stepper backwards from "rendering" to "booting" mid-render.
        """
        if goes_backwards(self.phase, phase):
            return
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
        }


JOBS: dict[str, Job] = {}


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


async def _run_job(job: Job) -> None:
    args = Namespace(
        workflow=str(ROOT / "workflows" / "wf.json"),
        **{k: v for k, v in job.params.items() if k not in ("cost", "timeout")},
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
        await client.close()


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
    no_upscale: bool = False
    remove_bg: str | None = None
    bg_refine: bool = False
    bg_sensitivity: float = 1.0
    bg_blur: int = 0
    bg_offset: int = 0
    cost: int = 100
    timeout: float = 900.0


app = FastAPI(title="mizuki console")


@app.post("/api/jobs")
async def create_job(req: JobRequest) -> dict:
    if req.remove_bg and req.remove_bg not in RMBG_MODELS:
        raise HTTPException(400, f"remove_bg must be one of {sorted(RMBG_MODELS)}")
    job = Job(id=uuid.uuid4().hex[:12], params=req.model_dump())
    JOBS[job.id] = job
    job.emit("submitting", "Request queued for the endpoint")
    asyncio.create_task(_run_job(job))
    return {"job_id": job.id}


@app.get("/api/jobs")
async def list_jobs() -> dict:
    ordered = sorted(JOBS.values(), key=lambda j: j.created, reverse=True)
    return {"jobs": [j.snapshot() for j in ordered[:50]]}


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
            if job.state in ("done", "error"):
                break
            await asyncio.sleep(1)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True))


if __name__ == "__main__":
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("mizuki console -> http://127.0.0.1:8800", file=sys.stderr)
    uvicorn.run(app, host="127.0.0.1", port=8800, log_level="warning")
