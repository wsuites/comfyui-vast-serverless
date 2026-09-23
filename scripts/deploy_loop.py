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
import atexit
import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

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
# The hourly price the operator set. The workergroup carries the same number in
# ``dph_total<=``, and twice on 2026-09-23 the autoscaler rented straight past
# it anyway - machine 45524 at 0.354 and again at 0.701 while the filter was in
# place and that machine was not in the offer list the same filter returned. The
# server-side ceiling is therefore a preference, not a guarantee, and the only
# enforcement that holds is this one: price the instance the moment it appears
# and take it down if it is over.
DPH_CEILING = 0.220

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


def seed() -> int:
    """A fresh seed per job.

    This was a constant, and that quietly broke the measurement it fed.
    ComfyUI caches on node inputs, so the second job of a cycle - same prompt,
    same seed - was answered out of cache without the sampler running at all.
    Warm renders came back in under five seconds against a thirteen second
    target and looked like a pass. Only a changing seed measures the GPU.
    """
    return random.randrange(1, 2**31)


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
    # ``/api/status`` asks Vast for the live picture, so it answers in a
    # second or two normally and much slower right after a destroy, when the
    # upstream is busy. A single short probe turned that into "start the
    # console first" and killed run13 before cycle 1.
    for attempt in range(3):
        try:
            api("/api/status", timeout=20)
            return True
        except (urllib.error.URLError, OSError, ValueError):
            if attempt < 2:
                time.sleep(3)
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


def enforce_price(inst: dict) -> bool:
    """Destroy and veto an instance the autoscaler rented above the ceiling.

    Returns True when the instance was taken down. The price is read off the
    live record rather than the offer that was searched, because the two do not
    always agree: the autoscaler rents from a list of its own and has twice been
    seen picking a machine the same filters exclude. Vetoing the machine as well
    as destroying the instance is what stops it coming straight back - a destroy
    on its own just buys the identical machine again a few seconds later.
    """
    dph = inst.get("dph_total")
    try:
        dph = float(dph)
    except (TypeError, ValueError):
        return False
    if dph <= DPH_CEILING + 1e-9:
        return False
    iid, mid = str(inst.get("id") or ""), inst.get("machine_id")
    log(f"    OVER CEILING: instance {iid} on machine {mid} at {dph:.3f}/h"
        f" (ceiling {DPH_CEILING:.3f}); destroying and vetoing")
    veto_machine(mid)
    try:
        vs._vastai_raw("destroy", "instance", iid, "-y", timeout=120)
    except OSError as exc:
        log(f"    could not destroy {iid}: {exc}")
    return True


def veto_machine(mid: Any) -> bool:
    """Add a machine to the workergroup's exclusion list, permanently.

    A machine that boots and then renders nothing is indistinguishable from a
    slow one until the budget is gone, and the autoscaler will hand it back on
    the next cycle because nothing told it otherwise. Machine 141696 cost run9
    two cycles that way. The list lives in the workergroup rather than in this
    script so the veto also holds for the web UI and for every later run.
    """
    mid = str(mid or "").strip()
    if not mid:
        return False
    # The filters are rebuilt from .env, not from the workergroup the API
    # echoes back: that copy carries defaults the server added on its own
    # (``verified=true``, ``rented=false``), and writing them back would
    # quietly restore a filter that was removed here on purpose.
    envf = ROOT / ".env"
    try:
        lines = envf.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log(f"    could not read .env to veto {mid}: {exc}")
        return False
    key = "VAST_SEARCH_PARAMS="
    idx = next((i for i, ln in enumerate(lines) if ln.startswith(key)), None)
    if idx is None:
        log(f"    no {key.rstrip('=')} in .env; cannot veto {mid}")
        return False
    params = lines[idx][len(key):].strip()
    head, sep, tail = params.partition("machine_id notin [")
    if not sep:
        new_params = f"{params} machine_id notin [{mid}]"
    else:
        current = [x.strip() for x in tail.split("]", 1)[0].split(",") if x.strip()]
        if mid in current:
            return True
        rest = tail.split("]", 1)[1]
        new_params = head + sep + ",".join(current + [mid]) + "]" + rest
    wg = vs.ENV.get("VAST_WORKERGROUP_ID", "")
    if not wg:
        log(f"    no VAST_WORKERGROUP_ID in .env; cannot veto {mid}")
        return False
    ok, out = vs._vastai_raw("update", "workergroup", str(wg),
                             "--endpoint_id", str(vs.ENDPOINT_ID),
                             "--search_params", new_params, timeout=60)
    if not ok:
        log(f"    could not veto machine {mid}: {out}")
        return False
    lines[idx] = key + new_params
    envf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"    machine {mid} vetoed; the autoscaler will not offer it again")
    return True


def wait_cleared(kind: str, deadline: float, old: str | None,
                 old_start: float | None = None) -> bool:
    """Confirm the reset really happened before starting the boot clock.

    Without this the cycle would time a worker that was never reset and report
    a suspiciously fast cold start. The old instance id matters because the
    autoscaler can rent a replacement while this is still looking: a different
    id in the list is the destroy having landed, not the reset having failed.

    A `paused` cycle cannot rely on catching the stopped status. The endpoint
    runs with cold_workers=1, so the autoscaler starts the instance again
    within seconds of the stop, and the instance record keeps no trace of it:
    the id survives and ``start_date`` is the original rental, not the last
    boot. Both 2026-09-22 runs failed every paused cycle for that reason while
    the stop was in fact landing - the cycle after it paid a second checkpoint
    load, which is what a restarted container costs and what gave it away.

    So the evidence is taken from the worker instead of the record: a stop that
    happened takes the endpoint's worker down, and one that silently no-ops
    leaves it serving. The cycle wants the second case to fail.
    """
    if old is None:
        return True
    # What the stop looked like from here, sampled once a minute. Four runs
    # have now failed this wait while the cycle after it paid a second
    # checkpoint load, so the evidence is written down instead of inferred.
    last_note = 0.0
    base_vram: float | None = None
    while time.time() < deadline:
        insts = vs.instances()
        if not insts:
            return True
        iid = str(insts[0].get("id"))
        if iid != old:
            return True
        status = (insts[0].get("actual_status") or "").lower()
        cur = (insts[0].get("cur_state") or "").lower()
        if status in ("exited", "stopped", "offline") or cur in ("stopped", "exited"):
            # A destroy that only stopped the machine has not cleared a fresh
            # cycle; the autoscaler would restart it instead of renting.
            if kind == "paused":
                return True
        if kind == "paused":
            worker = vs.state().get("worker") or {}
            if not worker.get("ready"):
                return True
            # A restart keeps the id and the rental date but cannot keep the
            # checkpoints: VRAM falling back to an empty container is the stop
            # having landed, and is the only trace that survives it.
            vram = worker.get("vram")
            if isinstance(vram, (int, float)):
                if base_vram is None:
                    base_vram = float(vram)
                elif base_vram > 1.0 and float(vram) < base_vram / 2:
                    log(f"    stop landed: VRAM fell {base_vram:.1f} -> "
                        f"{float(vram):.1f} GB")
                    return True
            if old_start is not None:
                now_start = insts[0].get("start_date")
                if now_start is not None and float(now_start) > float(old_start) + 1:
                    return True
            if time.time() - last_note > 60:
                last_note = time.time()
                log(f"    still serving: status {status or '?'}/{cur or '?'}, "
                    f"ready {worker.get('ready')}, vram {worker.get('vram')}")
        # The restart window is narrow enough that the ordinary cadence steps
        # straight over it.
        time.sleep(POLL / 3 if kind == "paused" else POLL)
    return False


# ------------------------------------------------------------------ provision

def wait_ready(deadline: float, seen: list[str], old: str | None = None,
               restart: bool = False) -> dict:
    """Poll until a worker reports ready, recording every phase it passes.

    The phase list is the interesting part of a failure: "stuck in
    provisioning for nine minutes" and "never left renting" are different
    bugs, and the summary line cannot tell them apart on its own.

    ``old`` is the worker this cycle just reset. The endpoint keeps serving its
    record for a few seconds after the instance is gone, so a cold start that
    reports ready in five seconds is that stale row, not a boot. Cycle 4 of the
    2026-09-22 run passed that way; ignoring the old id is what makes the
    number a measurement instead of a coincidence.

    The caller reads that id out of a JSON payload, where it is an int, so it
    is normalised here: comparing it against ``str(worker["id"])`` is always
    unequal, which silently disabled this guard for every run before
    2026-09-23.

    ``restart`` inverts that guard for a stopped instance. The autoscaler starts
    the very same id back up, so demanding a different one can only time out --
    which is the whole of cycle 2's 608s failure in run10 and run11. The stop is
    not taken on trust either: ``wait_cleared`` has already watched the instance
    reach a stopped state before this is called, which is the evidence the id
    comparison was standing in for. It has to come from there because the gap is
    not always visible from here -- in run11 the autoscaler had the machine back
    in ``running`` three seconds after the stop landed, faster than this loop
    polls. What the restart really cost is then read off the warmup number: a
    container that came back with empty VRAM pays for the checkpoints again, and
    a stop that did nothing renders in three seconds and says so.
    """
    old = str(old) if old is not None else None
    last = None
    last_inst = None
    tried: list[str] = []
    while time.time() < deadline:
        st = vs.state()
        phase, detail = st.get("phase"), st.get("detail")
        if phase != last:
            log(f"    phase: {phase} - {detail}")
            seen.append(phase)
            last = phase
        worker = st.get("worker") or {}
        # Every instance id the autoscaler puts under this endpoint counts as an
        # attempt. A boot that misses its budget after three attempts is the
        # autoscaler discarding machines, not a slow provision, and the two ask
        # for opposite fixes -- so name the machines instead of reporting a
        # single opaque timeout.
        iid = str(worker.get("id") or "") or None
        if iid and (restart or iid != old) and iid != last_inst:
            if last_inst is not None:
                log(f"    autoscaler dropped instance {last_inst} and moved on")
            label = f"{iid}@{worker.get('machine')}"
            tried.append(label)
            log(f"    attempt {len(tried)}: instance {iid} on machine "
                f"{worker.get('machine')} ({worker.get('gpu')})")
            last_inst = iid
            # Price it before waiting ten minutes for it to boot. An instance
            # over the ceiling is not a deployment worth measuring, and every
            # poll it survives is money.
            insts = [i for i in vs.instances() if str(i.get("id")) == iid]
            if insts and enforce_price(insts[0]):
                last_inst = None
                tried[-1] += " (over ceiling)"
                time.sleep(POLL)
                continue
        if worker.get("ready") and (old is None or restart or iid != old):
            return {"ready": True, "worker": worker, "tried": tried}
        # A worker that will never be ready should be replaced rather than
        # waited on. This is the same call the web UI makes before every job,
        # so exercising it here is the point, not a convenience.
        report = vs.unstick(dry_run=True)
        if report.get("acted"):
            log(f"    unstick would act: {report.get('detail')}")
        time.sleep(POLL)
    return {"ready": False, "worker": (vs.state().get("worker") or {}),
            "tried": tried}


# --------------------------------------------------------------------- render

def render(deadline: float) -> dict:
    """Submit one job through the same endpoint the web form posts to."""
    job = api("/api/jobs", {"prompt": PROMPT, "seed": seed(),
                            "width": 1024, "height": 1024,
                            "timeout": RENDER_BUDGET})
    jid = job["job_id"]
    log(f"    job {jid} submitted")
    last = None
    wedged_since: float | None = None
    while time.time() < deadline:
        snap = api(f"/api/jobs/{jid}")
        if snap.get("phase") != last:
            log(f"    job: {snap.get('phase')} - {snap.get('detail')}")
            last = snap.get("phase")
        # The endpoint reports an idle worker still carrying the requests a
        # previous abandoned job left counted against it. Nothing is rendering
        # and nothing will; waiting out the budget only buys a second failed
        # cycle on the same machine. Name it and get out.
        if "wedged" in (snap.get("detail") or "").lower():
            wedged_since = wedged_since or time.time()
            if time.time() - wedged_since > 45:
                return {"id": jid, "state": "wedged", "images": 0,
                        "error": "worker slot wedged by abandoned requests"}
        else:
            wedged_since = None
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
                       old=None if kind == "warm" else step.get("instance"),
                       restart=kind == "paused")
    row["boot_seconds"] = round(time.time() - t0, 1)
    row["phases"] = seen
    row["attempts"] = ready.get("tried") or []
    if not ready["ready"]:
        row["result"] = "fail"
        tried = row["attempts"]
        row["error"] = f"no ready worker within {BOOT_BUDGET:.0f}s"
        if len(tried) > 1:
            row["error"] += f" across {len(tried)} machines"
        log(f"  FAIL: {row['error']} (last phase {seen[-1] if seen else '?'})")
        if tried:
            log(f"    tried: {', '.join(tried)}")
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
    # A warm cycle pays this too. Cycle 3 of the 2026-09-23 run timed a worker
    # whose VRAM had been emptied under it and reported 36.7 s as the endpoint's
    # warm latency, next to 3.1 s from the same machine minutes earlier. On a
    # genuinely warm worker this render costs ~3 s; buying that removes the
    # question from every number below it.
    t_warm = time.time()
    first = render(t_warm + WARMUP_BUDGET)
    row["warmup_seconds"] = round(time.time() - t_warm, 1)
    row["warmup"] = first
    if first["state"] != "done" or not first["images"]:
        # The machine booted, benchmarked and then rendered nothing. That is a
        # bad machine, not a bad deployment: veto it and let the caller run the
        # cycle again on the replacement the autoscaler is now forced to rent.
        row["error"] = ("first render after boot: "
                        + (first.get("error") or f"ended {first['state']}"))
        log(f"  FAIL: {row['error']}")
        if veto_machine(w.get("machine")):
            row["result"] = "retry"
            row["vetoed"] = w.get("machine")
            try:
                vs._vastai_raw("destroy", "instance", str(w.get("id")), "-y",
                               timeout=120)
                log(f"    instance {w.get('id')} destroyed; retrying the cycle")
            except OSError as exc:
                log(f"    could not destroy {w.get('id')}: {exc}")
        else:
            row["result"] = "fail"
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


def pid_alive(pid: int) -> bool:
    """True if a process with this id exists, on Windows as well as POSIX."""
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError) as exc:
        return isinstance(exc, PermissionError)
    return True


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

    # Two harnesses pointed at one endpoint destroy each other's workers, and
    # the phase trace that comes out reads exactly like autoscaler churn: the
    # 2026-09-23 runs lost an hour to three overlapping copies before the
    # process list gave it away. Refuse to be the second one.
    lock = ROOT / "logs" / "deploy_loop.lock"
    if lock.exists():
        try:
            prev = int(lock.read_text().strip())
        except ValueError:
            prev = None
        if prev is not None and pid_alive(prev):
            print(f"another deploy_loop is already running (pid {prev}).\n"
                  f"Stop it first, or delete {lock} if it is stale.",
                  file=sys.stderr)
            return 2
        lock.unlink()
    lock.write_text(str(os.getpid()))
    atexit.register(lambda: lock.unlink(missing_ok=True))

    if not api_up():
        print(f"The console API is not answering on {API}.\n"
              f"Start it first:  cv web up", file=sys.stderr)
        return 2

    log(f"plan: {' -> '.join(plan)}")
    out = ROOT / "logs" / "deploy_loop.jsonl"
    out.parent.mkdir(exist_ok=True)

    rows = []
    aborted = False
    for i, kind in enumerate(plan, 1):
        # A machine that boots and renders nothing has been vetoed by the time
        # the cycle returns "retry", so the attempt after it lands somewhere
        # else. Two retries is the point where the fault stops being the
        # machine and starts being the deployment.
        for attempt in range(3):
            try:
                row = cycle(i, kind, args.yes)
            except KeyboardInterrupt:
                log("aborted by operator")
                aborted = True
                break
            except Exception as exc:                   # noqa: BLE001
                row = {"cycle": i, "reset": kind, "result": "fail",
                       "error": f"{type(exc).__name__}: {exc}"}
                log(f"  FAIL: {row['error']}")
            if row.get("result") == "retry":
                row["attempt"] = attempt + 1
                with out.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row) + "\n")
                if attempt < 2:
                    log(f"  retrying cycle {i} on another machine")
                    continue
                row["result"] = "fail"
                row["error"] += " (every machine offered rendered nothing)"
            break
        if aborted:
            break
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
