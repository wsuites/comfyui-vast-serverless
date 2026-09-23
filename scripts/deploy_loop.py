#!/usr/bin/env python3
"""Measure the deployment end to end, N times, and record what it costs.

The stack has three claims that only hold up under repetition: a cold start
lands under ten minutes, a warm render lands in seconds rather than minutes,
and the autoscaler recovers from every way a worker can be missing. This
script is what checks them. One cycle is:

    (operator resets the worker) -> the autoscaler notices -> a worker comes
    up -> a job renders

Resetting the worker is deliberately not automated. Destroying and stopping
instances spends money and is not something a measurement script should do on
its own; the script prints the exact command for the cycle it is about to run
and waits for the operator to confirm. What it does own is the clock and the
bookkeeping, which is the part that is tedious to do by hand five times.

Each cycle names the shape of reset it wants, covering the three the recovery
path has to handle:

    fresh   no instance at all; the autoscaler must rent one from scratch
    paused  an instance exists but is stopped; it must be started, not
            re-rented, which is the cheap path and the easy one to break
    warm    nothing is reset; this measures a render with no provisioning in
            front of it, which is the only honest read on request latency

A cycle passes when the worker became ready inside BOOT_BUDGET and the job
came back with an image. Everything measured lands in logs/deploy_loop.jsonl,
one object per cycle, so a later run can be compared against an earlier one
instead of against memory.

    python scripts/deploy_loop.py                 # 5 cycles, the default plan
    python scripts/deploy_loop.py --cycles 2
    python scripts/deploy_loop.py --plan warm,paused
    python scripts/deploy_loop.py --yes           # do not pause between cycles
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import vast_state as vs  # noqa: E402

# The cold-start budget the goal sets. Measured from the moment the reset is
# confirmed to the moment the worker reports ready, so it includes the
# autoscaler's own reaction time - the part nothing in this repo controls and
# the part most likely to blow the budget.
BOOT_BUDGET = 600.0
# How long a single render may take before the cycle is called failed. A warm
# 1024 render measures ~18 s on a 4090 with the face pass on; the ceiling is
# deliberately far above that, because the failure this catches is a job that
# hangs for a quarter of an hour, not one that is a few seconds slow.
RENDER_BUDGET = 300.0
# The first render after a boot additionally pays for the checkpoint load, so
# it gets its own ceiling. Measured at 60 s on a healthy 5080 and over 300 s on
# a machine that then served warm renders at 66 s - a slow warmup predicts a
# slow endpoint, which is why it is recorded rather than merely tolerated.
WARMUP_BUDGET = 420.0
# What a request is supposed to cost once the models are resident. Cycles are
# not failed on it - a passing cycle with a slow machine is a real deployment,
# and the run summary is where that shows up.
LATENCY_TARGET = 13.0
# The autoscaler polls on its own schedule; a reset is not visible to it
# instantly and a fresh instance does not appear the second it is asked for.
POLL = 15.0

API = "http://127.0.0.1:8800"

PLAN = ["fresh", "paused", "warm", "fresh", "warm"]

# What the operator has to do before each kind of cycle. Printed, never run.
RESET_HELP = {
    "fresh": ("Remove the current instance so the autoscaler has to rent a new\n"
              "    one:  cv instance destroy <id>"),
    "paused": ("Stop the current instance without releasing it, so the\n"
               "    autoscaler has to start it again:  cv instance stop <id>"),
    "warm": "Nothing to do - this cycle measures the worker as it stands.",
}

PROMPT = ("1girl, solo, standing, simple background, looking at viewer, "
          "detailed face")
SEED = 20260922


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def api(path: str, payload: dict | None = None, timeout: float = 30.0) -> dict:
    """One call against the local console API."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        API + path, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def api_up() -> bool:
    try:
        api("/api/status", timeout=5)
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


# --------------------------------------------------------------------- reset

def announce_reset(kind: str, auto: bool) -> dict:
    """Put the deployment into the state this cycle measures from.

    With ``auto`` the reset is performed here, which is what makes an
    unattended run possible; otherwise the command is printed and the operator
    does it by hand. Either way the money is spent knowingly: destroy throws a
    warm worker away and buys a cold start, so the caller asks for it.
    """
    insts = vs.instances()
    iid = str(insts[0].get("id")) if insts else None
    status = (insts[0].get("actual_status") or "?").lower() if insts else "none"
    # The restart of a stopped instance keeps the id and moves this, which is
    # the only durable trace a `paused` cycle leaves. See wait_cleared.
    started = insts[0].get("start_date") if insts else None
    log(f"  current: instance {iid or '(none)'}, status {status}")

    if kind == "warm":
        return {"kind": kind, "instance": iid, "note": "no reset requested"}
    if iid is None:
        # Nothing rented. A fresh cycle already has what it wants; a paused one
        # cannot stop what does not exist, and measuring the rent from scratch
        # is the closest honest thing to do.
        return {"kind": kind, "instance": None, "note": "nothing rented"}

    help_text = RESET_HELP[kind].replace("<id>", iid)
    print(f"    {help_text}")
    if not auto:
        try:
            input("    Press Enter once that is done (Ctrl-C to abort): ")
        except EOFError:
            log("  no tty; continuing without waiting")
        return {"kind": kind, "instance": iid, "note": "reset confirmed"}

    verb = "destroy" if kind == "fresh" else "stop"
    # Only `destroy` prompts, and only `destroy` takes -y: without it the CLI
    # reads the closed stdin, prints "Aborted." and exits 0 - a silent no-op
    # that would leave the next cycle measuring the worker it meant to replace.
    # `stop instance` takes the id alone and rejects -y as an unknown argument.
    args = (verb, "instance", iid) + (("-y",) if verb == "destroy" else ())
    ok, out = vs._vastai_raw(*args, timeout=120)
    if not ok:
        raise RuntimeError(f"{verb} {iid} failed: {out}")
    log(f"    {verb} sent to instance {iid}")
    return {"kind": kind, "instance": iid, "started": started, "note": f"{verb} sent"}


def wait_cleared(kind: str, deadline: float, old: str | None,
                 old_start: float | None = None) -> bool:
    """Confirm the reset really happened before starting the boot clock.

    Without this the cycle would time a worker that was never reset and report
    a suspiciously fast cold start. The old instance id matters because the
    autoscaler can rent a replacement while this is still looking: a different
    id in the list is the destroy having landed, not the reset having failed.

    A `paused` cycle cannot rely on catching the stopped status. The endpoint
    runs with cold_workers=1, so the autoscaler restarts the instance within
    seconds of the stop and the window is usually narrower than one poll - the
    2026-09-22 run failed every paused cycle waiting for a state that had
    already been and gone. The restart moves ``start_date`` while keeping the
    id, so a moved start is the same evidence arriving late.
    """
    if old is None:
        return True
    while time.time() < deadline:
        insts = vs.instances()
        if not insts:
            return True
        iid = str(insts[0].get("id"))
        if iid != old:
            return True
        status = (insts[0].get("actual_status") or "").lower()
        if status in ("exited", "stopped", "offline"):
            # A destroy that only stopped the machine has not cleared a fresh
            # cycle; the autoscaler would restart it instead of renting.
            if kind == "paused":
                return True
        if kind == "paused" and old_start is not None:
            now_start = insts[0].get("start_date")
            if now_start is not None and float(now_start) > float(old_start) + 1:
                return True
        time.sleep(POLL)
    return False


# ------------------------------------------------------------------ provision

def wait_ready(deadline: float, seen: list[str], old: str | None = None) -> dict:
    """Poll until a worker reports ready, recording every phase it passes.

    The phase list is the interesting part of a failure: "stuck in
    provisioning for nine minutes" and "never left renting" are different
    bugs, and the summary line cannot tell them apart on its own.

    ``old`` is the worker this cycle just reset. The endpoint keeps serving its
    record for a few seconds after the instance is gone, so a cold start that
    reports ready in five seconds is that stale row, not a boot. Cycle 4 of the
    2026-09-22 run passed that way; ignoring the old id is what makes the
    number a measurement instead of a coincidence.
    """
    last = None
    while time.time() < deadline:
        st = vs.state()
        phase, detail = st.get("phase"), st.get("detail")
        if phase != last:
            log(f"    phase: {phase} - {detail}")
            seen.append(phase)
            last = phase
        worker = st.get("worker") or {}
        if worker.get("ready") and (old is None or str(worker.get("id")) != old):
            return {"ready": True, "worker": worker}
        # A worker that will never be ready should be replaced rather than
        # waited on. This is the same call the web UI makes before every job,
        # so exercising it here is the point, not a convenience.
        report = vs.unstick(dry_run=True)
        if report.get("acted"):
            log(f"    unstick would act: {report.get('detail')}")
        time.sleep(POLL)
    return {"ready": False, "worker": (vs.state().get("worker") or {})}


# --------------------------------------------------------------------- render

def render(deadline: float) -> dict:
    """Submit one job through the same endpoint the web form posts to."""
    job = api("/api/jobs", {"prompt": PROMPT, "seed": SEED,
                            "width": 1024, "height": 1024,
                            "timeout": RENDER_BUDGET})
    jid = job["job_id"]
    log(f"    job {jid} submitted")
    last = None
    while time.time() < deadline:
        snap = api(f"/api/jobs/{jid}")
        if snap.get("phase") != last:
            log(f"    job: {snap.get('phase')} - {snap.get('detail')}")
            last = snap.get("phase")
        if snap["state"] in ("done", "error", "cancelled"):
            return {"id": jid, "state": snap["state"],
                    "latency": snap.get("latency"),
                    "elapsed": snap.get("elapsed"),
                    "images": len(snap.get("images") or []),
                    "error": snap.get("error")}
        time.sleep(3)
    # Give up on it properly. Leaving the job running would have the next
    # cycle measure a worker that is still busy with this one, and the whole
    # point of the run is that each number stands on its own.
    try:
        api(f"/api/jobs/{jid}/cancel", {})
        log("    job cancelled after exceeding the render budget")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log(f"    could not cancel {jid}: {exc}")
    return {"id": jid, "state": "timeout", "images": 0,
            "error": f"job never finished within {RENDER_BUDGET:.0f}s"}


# ---------------------------------------------------------------------- cycle

def cycle(n: int, kind: str, auto: bool) -> dict:
    log(f"=== cycle {n}: {kind} ===")
    row: dict = {"cycle": n, "reset": kind, "at": time.time()}

    step = announce_reset(kind, auto)
    row["reset_detail"] = step["note"]

    t0 = time.time()
    if kind != "warm":
        if not wait_cleared(kind, t0 + 300, step.get("instance"),
                            step.get("started")):
            row["result"] = "fail"
            row["error"] = "the worker was never reset; cycle not measurable"
            log(f"  FAIL: {row['error']}")
            return row
        log("  reset confirmed; waiting on the autoscaler")

    seen: list[str] = []
    # A warm cycle measures the worker that is already there, so the id it
    # starts from is the id it must see. The other two replaced it.
    ready = wait_ready(t0 + BOOT_BUDGET, seen,
                       old=None if kind == "warm" else step.get("instance"))
    row["boot_seconds"] = round(time.time() - t0, 1)
    row["phases"] = seen
    if not ready["ready"]:
        row["result"] = "fail"
        row["error"] = f"no ready worker within {BOOT_BUDGET:.0f}s"
        log(f"  FAIL: {row['error']} (last phase {seen[-1] if seen else '?'})")
        return row
    w = ready["worker"]
    row["worker"] = {"id": w.get("id"), "machine": w.get("machine"),
                     "gpu": w.get("gpu"), "dph": w.get("dph")}
    log(f"  ready in {row['boot_seconds']:.0f}s on {w.get('gpu')} "
        f"(machine {w.get('machine')})")

    # ``ready`` means the pyworker's benchmark passed, not that the workflow's
    # checkpoints are in VRAM: ComfyUI loads those on the first prompt that
    # asks for them. So the first render after a boot pays for ~7 GB of disk
    # reads and is not the number the endpoint serves all day. It is still a
    # real cost of the cold start, so it is measured - just as warmup, against
    # the boot budget, and the latency claim is read from the render after it.
    if kind != "warm":
        t_warm = time.time()
        first = render(t_warm + WARMUP_BUDGET)
        row["warmup_seconds"] = round(time.time() - t_warm, 1)
        row["warmup"] = first
        if first["state"] != "done" or not first["images"]:
            row["result"] = "fail"
            row["error"] = ("first render after boot: "
                            + (first.get("error") or f"ended {first['state']}"))
            log(f"  FAIL: {row['error']}")
            return row
        log(f"    models resident after {row['warmup_seconds']:.0f}s")

    t1 = time.time()
    job = render(t1 + RENDER_BUDGET)
    row["render_seconds"] = round(time.time() - t1, 1)
    row["job"] = job
    if job["state"] != "done" or not job["images"]:
        row["result"] = "fail"
        row["error"] = job.get("error") or f"job ended {job['state']}"
        log(f"  FAIL: {row['error']}")
        return row

    row["result"] = "pass"
    row["within_budget"] = (kind == "warm" or row["boot_seconds"] <= BOOT_BUDGET)
    lat = job.get("latency")
    row["on_target"] = bool(lat is not None and lat <= LATENCY_TARGET)
    warm = f", warmup {row['warmup_seconds']:.0f}s" if "warmup_seconds" in row else ""
    log(f"  PASS: boot {row['boot_seconds']:.0f}s{warm}, "
        f"render {row['render_seconds']:.0f}s "
        f"(endpoint latency {lat}"
        f"{'' if row['on_target'] else f' - over the {LATENCY_TARGET:.0f}s target'})")
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cycles", type=int, default=len(PLAN))
    ap.add_argument("--plan", help="comma list of fresh|paused|warm")
    ap.add_argument("--yes", action="store_true",
                    help="do not pause for confirmation between cycles")
    args = ap.parse_args()

    plan = args.plan.split(",") if args.plan else list(PLAN)
    bad = [p for p in plan if p not in RESET_HELP]
    if bad:
        print(f"unknown cycle kind(s): {bad}", file=sys.stderr)
        return 2
    while len(plan) < args.cycles:
        plan.append(PLAN[len(plan) % len(PLAN)])
    plan = plan[:args.cycles]

    if not api_up():
        print(f"The console API is not answering on {API}.\n"
              f"Start it first:  cv web up", file=sys.stderr)
        return 2

    log(f"plan: {' -> '.join(plan)}")
    out = ROOT / "logs" / "deploy_loop.jsonl"
    out.parent.mkdir(exist_ok=True)

    rows = []
    for i, kind in enumerate(plan, 1):
        try:
            row = cycle(i, kind, args.yes)
        except KeyboardInterrupt:
            log("aborted by operator")
            break
        except Exception as exc:                       # noqa: BLE001
            row = {"cycle": i, "reset": kind, "result": "fail",
                   "error": f"{type(exc).__name__}: {exc}"}
            log(f"  FAIL: {row['error']}")
        rows.append(row)
        with out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    print()
    print(f"{'#':>2}  {'cycle':7} {'result':7} {'boot':>7} {'warmup':>7} "
          f"{'lat':>7} {'machine':>8}  note")
    for r in rows:
        boot = f"{r['boot_seconds']:.0f}s" if r.get("boot_seconds") else "-"
        warm = f"{r['warmup_seconds']:.0f}s" if r.get("warmup_seconds") else "-"
        lat = (r.get("job") or {}).get("latency")
        lat = f"{lat:.1f}s" if isinstance(lat, (int, float)) else "-"
        mach = str((r.get("worker") or {}).get("machine") or "-")
        print(f"{r['cycle']:>2}  {r['reset']:7} {r.get('result','?'):7} "
              f"{boot:>7} {warm:>7} {lat:>7} {mach:>8}  {r.get('error','')}")
    passed = sum(1 for r in rows if r.get("result") == "pass")
    # Passing and being fast are separate claims: a cycle that boots and renders
    # on a machine sharing its GPU is a working deployment and a bad one.
    ontgt = sum(1 for r in rows if r.get("on_target"))
    print(f"\n{passed}/{len(rows)} cycles passed, "
          f"{ontgt}/{len(rows)} under the {LATENCY_TARGET:.0f}s latency target. "
          f"Log: {out}")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
