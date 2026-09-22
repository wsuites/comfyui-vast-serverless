#!/usr/bin/env python3
"""Live state of the serverless endpoint, as the Vast API really reports it.

Shared by ``webapp/server.py`` and ``scripts/mizuki.py`` so the phase rules
live in exactly one place.

Two views have to be merged, because neither is enough on its own:

* ``vastai show instances`` knows the machine, the price and the clock, but it
  says ``running`` for minutes while the worker is still downloading models.
* ``vastai get endpt-workers <id>`` knows ``ready_ever`` and ``reqs_working``,
  which is the only honest readiness signal, but carries no price.

Everything here is blocking on purpose; async callers push it to a thread.
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
from typing import Any, Iterator

from config import ENV

ENDPOINT_ID = ENV.get("VAST_ENDPOINT_ID", "")

# Phases in the order they happen. Kept here so the web stepper and the CLI
# agree on what comes after what.
PHASES = ["submitting", "renting", "booting", "provisioning",
          "generating", "saving", "done"]

LABELS = {
    "submitting": "queued",
    "renting": "renting GPU",
    "booting": "booting image",
    "provisioning": "loading models",
    "generating": "rendering",
    "saving": "fetching image",
    "done": "done",
    "error": "failed",
}


def goes_backwards(current: str, new: str) -> bool:
    """True when ``new`` would move the display to an earlier phase.

    Vast's ``actual_status`` flickers back to ``loading`` on a machine that is
    up and serving, which walks the stepper from "rendering" back to "booting"
    mid-render. Both the web page and the CLI have to suppress that, so the
    rule lives here rather than in each of them.

    Unknown phases (``error``) never count as going backwards.
    """
    if current not in PHASES or new not in PHASES:
        return False
    return PHASES.index(new) < PHASES.index(current)


def _vastai(*args: str, timeout: int = 30) -> Any:
    """Run a --raw Vast CLI call and parse it. None on any failure.

    Never raises: every caller here is a status display, and a status display
    that crashes is worse than one that says "unknown".
    """
    try:
        proc = subprocess.run(
            ["vastai", *args, "--raw"],
            capture_output=True, text=True, timeout=timeout,
        )
        return json.loads(proc.stdout)
    except Exception:
        return None


def instances() -> list[dict]:
    """Current instances. [] on any failure."""
    data = _vastai("show", "instances")
    return data if isinstance(data, list) else []


def workers() -> list[dict]:
    """What the autoscaler thinks of its workers. [] on any failure.

    Note the endpoint id is *positional*: ``--endpoint_id`` is not a flag the
    CLI accepts and it exits with a usage error.
    """
    if not ENDPOINT_ID:
        return []
    data = _vastai("get", "endpt-workers", str(ENDPOINT_ID))
    return data if isinstance(data, list) else []


def endpoint() -> dict:
    """The endpoint's own config (cold_workers, max_workers, timeouts)."""
    data = _vastai("show", "endpoints")
    if not isinstance(data, list):
        return {}
    for item in data:
        if str(item.get("id")) == str(ENDPOINT_ID):
            return item
    return data[0] if data else {}


def _number(value: Any) -> float | None:
    """Float, or None when the host reported nothing usable for the field."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------- wedged request slots
#
# ``reqs_working`` is the autoscaler's own counter, not a reading of the card. A
# request that dies between the autoscaler and ComfyUI - a client that hung up,
# a worker restarted mid-render - leaves the count incremented with nothing
# behind it. The endpoint then reports cur_load 100 for a worker that is doing
# nothing, routes no new work to it, and every later request sits in the queue
# until the client's own timeout fires.
#
# Measured 2026-09-22 on worker 52041444: status "idle", reqs_working 1,
# cur_load 100.0, gpu_util 0.0 at 36 C, held for over an hour. Two jobs died
# with "Timed out after 918.9s waiting for worker" and the card never warmed.
# A reboot clears it, but the count survives the restart by several minutes
# before the autoscaler reconciles it (see reboot()), so the stall clock going
# back to zero is the signal, not the worker coming back up.
#
# Detection needs patience, not cleverness. gpu_util comes from Vast's own
# polling (tens of seconds), so a request dispatched a moment ago legitimately
# reads "counted but idle" for a sample or two. Only a condition that survives
# STALL_AFTER seconds is called wedged, and the clock resets the instant any
# part of it stops holding.
STALL_AFTER = 150.0

_stall_since: float | None = None


# ------------------------------------------------------ worker reachability
#
# The autoscaler's router hands the client a raw address - ``https://<public
# ip>:<host port for 3000/tcp>`` - and the client posts the payload straight
# there. Vast never checks that address is reachable from where the client is
# sitting; the worker's own heartbeat goes out from the machine, so a host
# whose forwarded ports are firewalled still reports "ready".
#
# Measured 2026-09-22 on instance 52041444 (machine 142161): ICMP answered in
# 111 ms, every TCP port timed out - 42026 (ssh), 42033 (pyworker), 443. The
# route call returned that worker in 0.3 s. Every request then hung until the
# client's own timeout, leaving ``reqs_working`` incremented behind it. The
# wedged counter was the symptom; an unroutable machine was the cause.
#
# Probed with a bare TCP connect and never with the route call: routing
# reserves a request slot, so using it as a health check would wedge the very
# counter it is meant to diagnose.
#
# One exception, and it is the reason this probe reported a dead worker while
# renders kept coming back: when CF_WORKER_HOSTNAME is set, the onstart in
# renew_provisioning.py overrides PUBLIC_IPADDR/VAST_TCP_PORT_3000 before the
# pyworker imports metrics.get_url, so the url REPORTED to the autoscaler - and
# handed to the client - is the Cloudflare tunnel, not the host's ip:port. The
# instance record still carries the raw ip and the forwarded host port, which
# on those hosts is firewalled by definition (the tunnel exists because of it).
# Probing the record therefore always fails on exactly the machines the tunnel
# was added to rescue. Probe what the client will actually post to.
PROBE_PORT = "3000/tcp"
PROBE_TIMEOUT = 4.0
PROBE_EVERY = 30.0

# Kept in sync with the onstart by construction: both read the same variable,
# and the port is the 443 the onstart exports as VAST_TCP_PORT_3000.
CF_WORKER_HOSTNAME = ENV.get("CF_WORKER_HOSTNAME", "").strip()
CF_WORKER_PORT = 443

_probe_cache: tuple[float, str | None, bool | None] = (0.0, None, None)


def worker_address(inst: dict) -> str | None:
    """host:port the endpoint router would hand a client for this instance.

    With the worker tunnel enabled that is the tunnel hostname for every
    instance, because that is what the pyworker reports about itself.
    """
    if CF_WORKER_HOSTNAME:
        return f"{CF_WORKER_HOSTNAME}:{CF_WORKER_PORT}"
    ip = inst.get("public_ipaddr")
    mapping = (inst.get("ports") or {}).get(PROBE_PORT) or []
    port = mapping[0].get("HostPort") if mapping else None
    if not ip or not port:
        return None
    return f"{str(ip).strip()}:{port}"


def reachable(inst: dict) -> bool | None:
    """True/False if the worker's serving port answers. None if unknown.

    Cached for PROBE_EVERY seconds: describe() runs on every page poll and a
    connect timeout is four seconds of a request handler's life.
    """
    global _probe_cache
    target = worker_address(inst)
    if not target:
        return None
    age, cached_target, cached = _probe_cache
    now = time.time()
    if cached_target == target and now - age < PROBE_EVERY:
        return cached
    host, _, port = target.partition(":")
    try:
        sock = socket.create_connection((host, int(port)), timeout=PROBE_TIMEOUT)
        sock.close()
        ok: bool | None = True
    except OSError:
        ok = False
    except (TypeError, ValueError):
        ok = None
    _probe_cache = (now, target, ok)
    return ok


def _stalled_for(work: dict, gpu_util: float | None) -> float:
    """Seconds this worker has counted work while its GPU sat idle. 0 if not.

    Endpoint-wide on purpose: a wedged slot is a property of the worker, not
    of whichever job happens to be waiting behind it.
    """
    global _stall_since

    wedged = (bool(work.get("reqs_working"))
              and (work.get("status") or "").lower() == "idle"
              and gpu_util is not None and gpu_util <= 0)
    if not wedged:
        _stall_since = None
        return 0.0
    now = time.time()
    if _stall_since is None:
        _stall_since = now
    return now - _stall_since


def describe(insts: list[dict], works: list[dict]) -> dict:
    """Merge both views into a single phase, detail and worker record."""
    if not insts and not works:
        return {
            "phase": "renting",
            "detail": "No machine yet - the autoscaler is looking for an offer",
            "worker": None,
        }

    inst = insts[0] if insts else {}
    work = works[0] if works else {}
    start = inst.get("start_date") or work.get("started_at")
    dph = inst.get("dph_total")
    worker = {
        "id": inst.get("id") or work.get("id"),
        "gpu": inst.get("gpu_name") or work.get("gpu_name"),
        "dph": dph,
        "machine": inst.get("machine_id"),
        "start": start,
        "status": work.get("status"),
        "ready": bool(work.get("ready_ever")),
        "reqs": work.get("reqs_working") or 0,
        # Telemetry the host reports to Vast, not a live meter: it is refreshed
        # on Vast's own polling cadence (tens of seconds), so a 20 s render can
        # start and finish between two samples and never show up as load. Read
        # it as "is the card busy at all", never as a per-step progress bar.
        #
        # gpu_util is a percentage, 0-100. cpu_util from the same record is
        # deliberately not relayed: it has been seen as both 0.216 and 1.167 on
        # this endpoint, so its unit is not knowable from here, and a number
        # whose scale is a guess is worse than no number.
        "gpu_util": _number(inst.get("gpu_util")),
        "gpu_temp": inst.get("gpu_temp"),
        # GB in use against the card's total, which Vast reports in MB.
        "vram": inst.get("vmem_usage"),
        "vram_total": (float(inst["gpu_totalram"]) / 1024
                       if inst.get("gpu_totalram") else None),
    }
    if start:
        worker["hours"] = max(0.0, (time.time() - float(start)) / 3600)
        worker["spent"] = worker["hours"] * float(dph or 0)

    status = (inst.get("actual_status") or "").lower()
    msg = (inst.get("status_msg") or "").strip()

    stalled = _stalled_for(work, worker["gpu_util"])
    worker["stalled"] = stalled or None
    worker["address"] = worker_address(inst)
    worker["reachable"] = reachable(inst)

    # Before anything about phases: if the serving port does not answer from
    # here, the client cannot post to this worker whatever Vast believes. Say
    # that instead of "Rendering", because the request is going to sit in the
    # queue until it times out and no restart of ComfyUI will change it.
    if worker["reachable"] is False:
        # Two different failures share this branch, and they want opposite
        # remedies: a firewalled host is disposable, a down tunnel is not -
        # replacing the machine would just rebuild the same broken edge.
        if CF_WORKER_HOSTNAME:
            detail = (f"Worker tunnel down at {worker['address']} - the "
                      f"pyworker reports this hostname to the autoscaler and "
                      f"it does not answer. Check cloudflared on the worker")
        else:
            detail = (f"Worker unreachable at {worker['address']} - the "
                      f"machine answers ping but its forwarded ports do not. "
                      f"Requests cannot be delivered; replace it")
        return {"phase": "generating" if work.get("reqs_working") else "provisioning",
                "detail": detail,
                "worker": worker}

    # Order matters here, and it is the opposite of the obvious one.
    #
    # Work in flight beats every other signal - once it has been corroborated.
    # A counted request with a cold card is not a render (see STALL_AFTER); say
    # so, because the alternative is a progress line that reads "Rendering" for
    # the fifteen minutes it takes the request to time out in the queue.
    if work.get("reqs_working"):
        if stalled > STALL_AFTER:
            return {"phase": "generating",
                    "detail": (f"Worker idle with {work['reqs_working']} "
                               f"request(s) still counted against it for "
                               f"{stalled / 60:.0f} min - the slot is wedged, "
                               f"nothing is rendering"),
                    "worker": worker}
        return {"phase": "generating",
                "detail": f"Rendering - {work['reqs_working']} request(s) in flight",
                "worker": worker}

    # ``ready_ever`` is checked BEFORE ``actual_status`` on purpose. Both of
    # Vast's liveness fields lie in the same direction: ``status`` reports
    # "offline" and ``actual_status`` flips back to "loading" on a worker that
    # answers /health in 0.4s, whenever its heartbeat to the autoscaler lapses.
    # Measured 2026-09-20 on instance 51742546, which had been serving for 57
    # minutes and was still being reported as "booting the image".
    # Once a worker has been ready, only work in flight can change the story.
    if work.get("ready_ever"):
        return {"phase": "generating",
                "detail": "Worker ready - dispatching the request",
                "worker": worker}

    # Never ready so far, so an unusual status really is a boot in progress.
    if status and status != "running":
        return {"phase": "booting",
                "detail": msg or f"Machine {status} - pulling the image",
                "worker": worker}

    return {"phase": "provisioning",
            "detail": "Machine up - downloading models and starting ComfyUI",
            "worker": worker}


def state() -> dict:
    """One call: both views, merged."""
    return describe(instances(), workers())


# ------------------------------------------------------- stranded recovery
#
# Stopping an on-demand instance keeps the DISK but releases the GPU. If
# another tenant takes that GPU while we are stopped, the instance can never
# start again: it is pinned to one machine, and that machine is now full.
# With max_workers=1 the autoscaler does not rent a replacement elsewhere, so
# the endpoint sits there accepting requests it cannot serve.
#
# Measured 2026-09-21 on instance 51766427 (machine 51021). Vast reported
# `endpoint_state: active`, counted every request (`request_idx: 370`) and
# still answered `total workers: 1 loading workers: 0`. Nothing in the status
# fields says why. The decisive answer only came from trying to start it:
#
#     $ vastai start instance 51766427
#     Required resources are currently unavailable, state change queued.
#     $ vastai search offers "machine_id=51021"   -> 0
#
# Which is why detection here does NOT guess from status fields. Those lie in
# both directions (see describe()). It asks Vast to start the thing and reads
# the refusal. Cheap, unambiguous, and it cannot fire on a healthy worker.

# Vast's phrasing when the host has no free GPU for a stopped instance.
_NO_RESOURCES = ("required resources are currently unavailable",
                 "no longer available", "not available")


def _vastai_raw(*args: str, timeout: int = 60) -> tuple[bool, str]:
    """Run a mutating Vast CLI call. Returns (ok, combined output).

    Unlike ``_vastai`` this reports failure instead of swallowing it: these
    calls destroy and create rented hardware, and a silent failure there is
    how you end up paying for two workers or none.
    """
    try:
        proc = subprocess.run(
            ["vastai", *args],
            capture_output=True, text=True, timeout=timeout,
            # Never inherit a terminal. ``destroy instance`` prompts for
            # confirmation, and a prompt reading an inherited stdin hangs the
            # caller forever; reading an empty one silently aborts while
            # still exiting 0. Both are answered by -y plus DEVNULL here.
            stdin=subprocess.DEVNULL,
        )
        out = f"{proc.stdout}\n{proc.stderr}".strip()
        # An aborted confirmation exits 0, so the text is the only signal.
        ok = proc.returncode == 0 and "aborted" not in out.lower()
        return ok, out
    except Exception as exc:
        return False, str(exc)


def free_offers(machine_id: Any) -> int | None:
    """How many rentable offers the machine still has. None if unknown.

    Corroborates a start refusal: 0 means the host really is full, rather
    than Vast having a transient wobble.
    """
    if not machine_id:
        return None
    data = _vastai("search", "offers", f"machine_id={machine_id}")
    return len(data) if isinstance(data, list) else None


def stranded(inst: dict, probe: bool = True) -> str | None:
    """Reason the instance cannot come back, or None if it is fine.

    The probe is a real ``vastai start``, which means a stopped-but-healthy
    instance gets *started* by asking. That is the wanted outcome before a
    render, but it is a side effect, so ``probe=False`` answers read-only
    from the offer count instead - less certain, and used by --dry-run so
    that a dry run cannot spend money.
    """
    status = (inst.get("actual_status") or "").lower()
    if status not in ("exited", "stopped", "offline", ""):
        return None
    # An instance on its way up is not stranded, it is slow.
    if (inst.get("next_state") or "").lower() == "running":
        return None

    iid = inst.get("id")
    if not probe:
        free = free_offers(inst.get("machine_id"))
        if free == 0:
            return (f"instance {iid} is {status or 'not running'} and machine "
                    f"{inst.get('machine_id')} has 0 free offers (read-only "
                    f"guess: no start was attempted)")
        return None
    ok, out = _vastai_raw("start", "instance", str(iid))
    low = out.lower()
    if any(p in low for p in _NO_RESOURCES):
        free = free_offers(inst.get("machine_id"))
        detail = f", machine {inst.get('machine_id')} has {free} free offers" \
            if free is not None else ""
        return f"{out.splitlines()[0].strip()}{detail}"
    if not ok:
        return None      # some other failure: do not destroy on a guess
    return None          # it accepted the start, so it was merely stopped


def replace(inst: dict, reason: str, dry_run: bool = False) -> dict:
    """Destroy a stranded instance so the autoscaler rents another machine.

    This is the whole point: the worker is pinned to a host that has no GPU
    left, and the only way off that host is to stop existing on it. The
    autoscaler then re-rents from the pool using the workergroup's search
    params, which is exactly what it does on a fresh deploy.

    COST: the replacement re-downloads every model from R2 on first boot, so
    this trades a ~7 minute cold start for an endpoint that works at all.
    """
    iid = inst.get("id")
    report = {"instance": iid, "machine": inst.get("machine_id"),
              "reason": reason, "destroyed": False, "detail": ""}
    if dry_run:
        report["detail"] = "dry run: would destroy and let the autoscaler re-rent"
        return report
    # -y is mandatory: without it the CLI prompts, reads the closed stdin,
    # prints "Aborted." and exits 0, so the replacement silently never happens.
    ok, out = _vastai_raw("destroy", "instance", str(iid), "-y")
    report["destroyed"] = ok
    report["detail"] = out or ("destroyed" if ok else "destroy failed")
    return report


def unstick(dry_run: bool = False) -> dict:
    """Detect and fix a worker stranded on a full machine. Safe to call often.

    Returns a report with ``acted`` False when there was nothing wrong, which
    is the common case: the probe costs one CLI call and never touches a
    machine that can still start.
    """
    insts = instances()
    if not insts:
        return {"acted": False,
                "detail": "no instance: the autoscaler is free to rent one"}

    inst = insts[0]
    works = workers()
    work = works[0] if works else {}

    # Work in flight, or a worker that has been ready, is not stranded no
    # matter what the instance record says. Same precedence as describe().
    if work.get("reqs_working") or work.get("ready_ever"):
        if (inst.get("actual_status") or "").lower() == "running":
            return {"acted": False, "detail": "worker is serving"}

    reason = stranded(inst, probe=not dry_run)
    if not reason:
        return {"acted": False,
                "detail": f"instance {inst.get('id')} can still start"}

    report = replace(inst, reason, dry_run=dry_run)
    report["acted"] = True
    return report


def reboot(instance_id: str | int | None = None) -> dict:
    """Stop/start the worker's container. The way to clear a wedged slot.

    It works, but not on the clock you expect. Measured 2026-09-22 on worker
    52041444, held at ``reqs_working 1 / cur_load 100`` with the GPU idle for
    over an hour:

    * 05:24 reboot issued, 05:25 ``status: rebooting``
    * 05:25 worker back (``started_at`` moves), still ``reqs_working 1``
    * 05:28 still 1 - the count outlives the process that earned it
    * 05:33 ``reqs_working 0``, ``cur_load 0``, status Ready

    So the counter is the autoscaler's accounting, reconciled on its own
    cadence, and the window between "the worker is back" and "the slot is
    free" is minutes long. Do not read a reboot as failed at three minutes;
    watch until the count drops. The
    container keeps its disk, so the models do not download again - but the
    stop does release the GPU, and a machine that fills in that window leaves
    the instance stranded. ``unstick()`` is the way out of that, and the
    caller should be told, which is why the hazard is in the return value and
    not only in this comment.
    """
    insts = instances()
    target = str(instance_id or (insts[0].get("id") if insts else "") or "")
    if not target:
        return {"ok": False, "detail": "no instance to reboot"}

    ok, out = _vastai_raw("reboot", "instance", target, timeout=120)
    # The stall clock is measuring a worker that is about to stop existing.
    global _stall_since
    _stall_since = None
    return {
        "ok": ok,
        "instance": target,
        "detail": (out.strip().splitlines() or ["rebooting"])[-1] if ok else out.strip(),
        "hazard": ("The stop releases the GPU. If the machine fills before the "
                   "start, the instance is stranded and has to be replaced."),
    }

# ------------------------------------------------------------------ results


def walk(node: Any) -> Iterator[dict]:
    """Yield every dict in a nested structure, parsing embedded JSON strings.

    The worker wraps its real answer as a JSON *string* inside the envelope, so
    a plain dict walk misses the output list entirely.
    """
    if isinstance(node, str):
        stripped = node.strip()
        if stripped[:1] in "{[":
            try:
                yield from walk(json.loads(stripped))
            except Exception:
                pass
    elif isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)


def extract_image_urls(result: Any) -> list[str]:
    """Collect presigned image URLs from the worker's reply, in order.

    Filtered by extension so the envelope's own ``url`` (the worker endpoint)
    does not get mistaken for a result.
    """
    urls: list[str] = []
    for entry in walk(result):
        url = entry.get("url")
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        name = url.split("?", 1)[0].lower()
        if name.endswith((".png", ".jpg", ".jpeg", ".webp")) and url not in urls:
            urls.append(url)
    return urls
