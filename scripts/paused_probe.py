#!/usr/bin/env python3
"""Reproduce the stopped -> running case in isolation, until it is understood.

run15 cycle 2 is the only goal criterion with no live evidence. What it showed:

    reset: instance 52198605 stopped
    worker 52198605 is exited; starting it again
    instance 52198605 went exited on its own      <- 25s after an accepted start
    instance 52198605 destroyed - exited

``start`` returned success and the container died on its own a few seconds
later. fleet.py then treated that as a dead host and rented fresh, so the run
still passed - it just never measured a restart. The information that would
explain it lives in the instance log, which the normal path destroys before
reading.

This probe stops and restarts the SAME instance repeatedly. On any failure it
pulls the container log FIRST and only then releases the hardware. It never
vetoes: a restart that fails is not evidence about the host, and writing
guesses into .env is what cost the best card earlier today.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import fleet  # noqa: E402

LOG = ROOT / "logs" / "paused_probe.jsonl"
STOP_WAIT = 180.0
READY_CAP = 600.0
POLL = 5.0
GRACE = 45.0  # how long the API may keep reporting the pre-start status


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def status_of(iid: str) -> str:
    inst = fleet.find(iid)
    if inst is None:
        return "gone"
    return (inst.get("actual_status") or inst.get("cur_state") or "").lower() or "unknown"


def container_log(iid: str, tail: int = 300) -> str:
    """The instance log, fetched while the instance still exists.

    Not routed through ``fleet.cli``: container logs carry arbitrary bytes and
    Python decodes a subprocess pipe as cp1252 on this box, which raises inside
    the reader thread and hands back None. The whole point of this function is
    to survive whatever the container printed, so it decodes permissively.
    """
    try:
        proc = subprocess.run(
            [fleet.vs.vastai_bin() or "vastai",
             "logs", str(iid), "--tail", str(tail)],
            capture_output=True,
            timeout=120,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            stdin=subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001
        return f"(log fetch failed: {type(exc).__name__}: {exc})"
    raw = (proc.stdout or b"") + (proc.stderr or b"")
    text = raw.decode("utf-8", errors="replace").strip()
    return text or f"(empty log; rc={proc.returncode})"


def wait_stopped(iid: str) -> str:
    deadline = time.time() + STOP_WAIT
    while time.time() < deadline:
        st = status_of(iid)
        if st in ("stopped", "exited", "gone"):
            return st
        time.sleep(3)
    return status_of(iid)


def watch_restart(iid: str, machine: str) -> dict:
    """Poll a restarted instance to either serving or death.

    Distinguishes the two outcomes the harness conflated: a container that
    exits on its own after an accepted start, and one that stays up but never
    answers. Returns the timeline either way.

    The status right after an accepted ``start`` is still the pre-start value -
    the API reports ``exited`` for a while before it reflects the transition.
    Reading that as death is how the first version of this probe "proved" a
    restart failed in 0.0 seconds. So a dead status only counts once the
    instance has been seen alive, or once GRACE has passed, and then only if it
    holds for two consecutive polls.
    """
    started = time.time()
    deadline = started + READY_CAP
    published_at = None
    seen = []
    last = None
    ever_alive = False
    dead_streak = 0

    while time.time() < deadline:
        elapsed = round(time.time() - started, 1)
        st = status_of(iid)
        if st != last:
            seen.append({"t": elapsed, "status": st})
            log(f"  +{elapsed:.0f}s status={st}")
            last = st

        if st in ("running", "loading", "created", "starting"):
            ever_alive = True

        if st in ("exited", "stopped", "gone"):
            dead_streak += 1
            settled = ever_alive or elapsed >= GRACE
            if settled and dead_streak >= 2:
                return {
                    "outcome": "died",
                    "seconds": elapsed,
                    "status": st,
                    "ever_alive": ever_alive,
                    "timeline": seen,
                }
        else:
            dead_streak = 0

        if st == "running":
            inst = fleet.find(iid) or {}
            url = fleet.comfy_url(inst)
            if url and published_at is None:
                published_at = elapsed
                log(f"  +{elapsed:.0f}s port published -> {url}")
            if url and fleet.comfy_ready(url):
                return {
                    "outcome": "ready",
                    "seconds": elapsed,
                    "published_at": published_at,
                    "url": url,
                    "timeline": seen,
                }

        time.sleep(POLL)

    return {
        "outcome": "timeout",
        "seconds": round(time.time() - started, 1),
        "published_at": published_at,
        "timeline": seen,
    }


def cycle(idx: int, keep: bool) -> dict:
    row: dict = {"attempt": idx, "ts": time.time()}

    rec = fleet.up()
    if not rec:
        row.update(outcome="no_worker")
        log("FAIL: could not bring a worker up at all")
        return row

    iid, mid = str(rec["instance"]), str(rec["machine"])
    row.update(instance=iid, machine=mid, gpu=rec.get("gpu"), dph=rec.get("dph"))
    log(f"worker {iid} serving on {rec.get('gpu')} (machine {mid})")

    ok, out = fleet.cli("stop", "instance", iid, timeout=120)
    if not ok:
        row.update(outcome="stop_refused", detail=out[:300])
        log(f"FAIL: stop refused: {out.strip()[:200]}")
        return row

    st = wait_stopped(iid)
    row["stopped_as"] = st
    log(f"stop landed as {st}")
    if st == "gone":
        # Vast removed it rather than stopping it. Nothing to restart.
        row.update(outcome="vanished_on_stop")
        log("FAIL: instance vanished on stop - there is nothing to restart")
        return row

    t0 = time.time()
    ok, out = fleet.cli("start", "instance", iid, timeout=120)
    row["start_accepted"] = ok
    if not ok:
        row.update(outcome="start_refused", detail=out[:300])
        log(f"FAIL: start refused: {out.strip()[:200]}")
        fleet.destroy(iid, "start refused in probe")
        return row
    log(f"start accepted after {round(time.time() - t0, 1)}s; watching")

    res = watch_restart(iid, mid)
    row.update(res)

    if res["outcome"] != "ready":
        # Read the box before letting go of it. This is the whole point.
        log(f"FAIL: restart {res['outcome']} after {res['seconds']}s - pulling log")
        blob = container_log(iid)
        row["container_log"] = blob[-4000:]
        dump = ROOT / "logs" / f"paused_probe_{iid}_{idx}.log"
        dump.write_text(blob, encoding="utf-8")
        log(f"container log -> {dump}")
        fleet.destroy(iid, f"probe: restart {res['outcome']}")
    else:
        log(f"PASS: restart served in {res['seconds']}s (port at {res.get('published_at')}s)")
        if not keep:
            fleet.destroy(iid, "probe cycle finished")

    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--attempts", type=int, default=3)
    ap.add_argument("--keep", action="store_true", help="leave a passing worker running")
    args = ap.parse_args()

    LOG.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for i in range(1, args.attempts + 1):
        log(f"=== restart attempt {i}/{args.attempts} ===")
        try:
            row = cycle(i, keep=args.keep and i == args.attempts)
        except KeyboardInterrupt:
            log("aborted by operator")
            return 130
        except Exception as exc:  # noqa: BLE001
            row = {"attempt": i, "outcome": "error", "error": f"{type(exc).__name__}: {exc}"}
            log(f"FAIL: {row['error']}")
        rows.append(row)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")

    print()
    print(f" {'#':>2}  {'outcome':<18} {'secs':>7}  {'machine':<9} gpu")
    for r in rows:
        print(
            f" {r['attempt']:>2}  {str(r.get('outcome', '-')):<18} "
            f"{str(r.get('seconds', '-')):>7}  {str(r.get('machine', '-')):<9} "
            f"{str(r.get('gpu', '-'))}"
        )
    good = sum(1 for r in rows if r.get("outcome") == "ready")
    print(f"\n{good}/{len(rows)} restarts served. Log: {LOG}")
    return 0 if good == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
