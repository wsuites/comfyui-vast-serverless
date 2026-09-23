#!/usr/bin/env python3
"""Run the web page's job path against the fake Vast and fake ComfyUI.

``test_fleet_loop.py`` proves the deployment loop. This proves the thing the
operator actually clicks: ``webapp/server.py`` used to hand jobs to the Vast
serverless endpoint, and the rewrite points it at ``fleet.py`` instead. That
swap is only worth anything if a job still ends with pixels on disk, so the
same fakes drive it end to end here - a render, and a cancel that has to reach
ComfyUI's /interrupt rather than quietly dropping the client side of the wire.

    python scripts/test_webapp_dispatch.py
"""

from __future__ import annotations

import asyncio
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "webapp"))

from test_fleet_loop import Comfy, Vast, install   # noqa: E402


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Comfy)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    v = Vast(port)
    fake = install(v)

    import server

    # The page's module-level ``fleet`` is the only seam: swapping it here
    # means every call site inside _run_job is exercised as written, including
    # the ones a narrower stub would have skipped past.
    server.fleet = fake
    # The infra watcher polls the real Vast CLI, which is not what is under
    # test and would spend a minute of subprocess timeouts saying nothing.
    async def _quiet(job):
        while True:
            await asyncio.sleep(3600)
    server._watch_infra = _quiet

    fails: list[str] = []

    # Built from the request model the page actually posts, so a field added
    # there cannot drift away from what this suite exercises.
    params = server.JobRequest(prompt="a test").model_dump()

    # --- 1. a job that should finish -------------------------------------
    job = server.Job(id="testdispatch01", params=dict(params))
    asyncio.run(server._run_job(job))

    if job.state != "done":
        fails.append(f"render job ended {job.state}: {job.error}")
    if not job.images:
        fails.append("render job produced no images")
    for rel in job.images:
        path = server.OUT_DIR / rel[len("/results/"):]
        if not path.is_file() or not path.stat().st_size:
            fails.append(f"image {rel} was not written to disk")
    if job.latency is None:
        fails.append("render job never recorded a latency")
    if not v.rentals:
        fails.append("the job never rented anything")
    phases = [e["phase"] for e in job.log]
    if [p for p in ("renting", "generating", "saving", "done") if p not in phases]:
        fails.append(f"phase trail is not a deployment: {phases}")

    # --- 2. a job that gets cancelled mid-render --------------------------
    # The old path could not interrupt: the request sat behind the serverless
    # router and the worker finished anyway. If this stops passing, the page is
    # lying to the operator about what a cancel costs.
    before = Comfy.interrupts
    # The fake renders in about a second, which leaves no window to cancel in.
    # Stretch a render out so the cancel has to land mid-flight, the way it
    # does against a real eighty-second graph.
    import test_fleet_loop as tfl
    tfl.NODE_COST = {k: v * 40 for k, v in tfl.NODE_COST.items()}
    tfl.NODE_COST_DEFAULT *= 40

    async def cancel_run() -> None:
        job2 = server.Job(id="testdispatch02", params=dict(params))
        task = asyncio.create_task(server._run_job(job2))
        # Wait for the render to be genuinely in flight: the trail says where we
        # are, and "generating" is the phase that only exists once a prompt
        # is actually queued on the card.
        for _ in range(400):
            await asyncio.sleep(0.05)
            if any(e["phase"] == "generating" for e in job2.log):
                break
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        if job2.state != "cancelled":
            fails.append(f"cancelled job ended {job2.state}")

    asyncio.run(cancel_run())
    if Comfy.interrupts <= before:
        fails.append("cancelling never reached ComfyUI's /interrupt")

    srv.shutdown()
    print()
    for f in fails:
        print("FAIL " + f)
    print("the web page deploys and renders on its own workers"
          if not fails else "FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
