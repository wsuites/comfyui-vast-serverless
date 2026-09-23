#!/usr/bin/env python3
"""Our own autoscaler: rent, verify, dispatch to and release a ComfyUI worker.

Why this exists, in one paragraph. The Vast serverless endpoint couples two
unrelated things into a single call: ``endpoint._route(cost, req_idx)`` is both
the demand signal that makes the autoscaler rent hardware *and* the reservation
that hands a slot to a client. When a request dies between those two - a
firewalled host, a worker that never finishes booting, a client timeout - the
reservation stays counted in ``reqs_working`` while nothing is running, the
slot is wedged, and every later request queues behind a worker that will never
report progress. Measured across five distinct machines over runs 13 and 14,
always as ``TimeoutError: Timed out after 3xx s waiting for worker``, so it is
the router and not the hosts.

This module separates the two concerns. Demand is a local counter we own.
Renting is an offer search we rank ourselves. Dispatch goes straight to
ComfyUI's HTTP API on the instance, so a dead client leaks nothing: there is no
reservation to abandon, and a cancel is a real ``/interrupt`` rather than a
request the router forgets about. A worker is only ever declared ready after we
have personally fetched ``/object_info`` from it and seen our own checkpoint in
the list, which is the check that would have rejected every wedged host at rent
time instead of ten minutes later.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import suppress
from pathlib import Path
from typing import Any, Iterable

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import config  # noqa: E402
import vast_state as vs  # noqa: E402

ENV = config.ENV

# --- policy ------------------------------------------------------------------

# The operator's hourly ceiling. Vast's own ``dph_total<=`` filter is advisory:
# on 2026-09-23 the serverless autoscaler rented machine 45524 at 0.354 and
# again at 0.701 while that filter was live, and that machine was not in the
# offer list the same filter returned. So the ceiling is enforced here, twice:
# once when ranking offers, and once against the instance record after it
# exists, because those two disagree.
DPH_CEILING = float(ENV.get("FLEET_DPH_CEILING", "0.220"))

# How long a cold machine gets to download models and answer /object_info.
# The goal is under ten minutes; this is the hard stop, not the target.
BOOT_CAP = float(ENV.get("FLEET_BOOT_CAP", "600"))

# A rented instance that has served nothing for this long is released. Vast
# bills by the second while running, so the only thing standing between an
# abandoned session and an overnight bill is this number.
IDLE_AFTER = float(ENV.get("FLEET_IDLE_AFTER", "600"))

POLL = 5.0
PROBE_TIMEOUT = 6.0
# How long the API may keep reporting an instance's pre-start status after an
# accepted start. Measured up to 46s across restarts that then served fine.
STATUS_GRACE = 90.0

TEMPLATE_ID = int(ENV.get("VAST_TEMPLATE_ID", "0") or 0)
CREATOR_ID = ENV.get("VAST_CREATOR_ID", "")
DISK = ENV.get("VAST_DISK_SPACE", "26")
# Changing this while an instance is rented orphans it: ours() matches on the
# label, so the old one stops being found, stops being reaped, and bills until
# someone kills it by hand. Release the fleet first, then rename.
LABEL = ENV.get("FLEET_LABEL", "pyworker-vast-fleet")

# ComfyUI's port inside the container. Set by the template as
# ``COMFYUI_ARGS="--disable-auto-launch --port 18188"`` and asserted by the
# provisioning script's own liveness check, which curls 127.0.0.1:18188.
COMFY_PORT = 18188
COMFY_KEY = f"{COMFY_PORT}/tcp"

# Readiness means this file is loadable, not merely that the server answers.
# ComfyUI binds its port before provisioning has finished fetching weights, so
# a plain 200 on /object_info is true several minutes before the worker can
# actually render anything.
READY_CHECKPOINT = ENV.get("FLEET_CHECKPOINT", "waiIllustriousSDXL_v170.safetensors")

STATE_PATH = ROOT / "logs" / "fleet.json"
LEASE_PATH = ROOT / "logs" / "fleet.lease"

# How long a lease stands without a refresh. Short on purpose: the loops that
# hold one refresh every few seconds, so the only thing this number sizes is
# how long an orphan outlives the process that was working on it. Make it
# generous and a crashed API buys exactly the overnight bill IDLE_AFTER exists
# to prevent.
LEASE_TTL = float(ENV.get("FLEET_LEASE_TTL", "120"))


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --- vast cli ----------------------------------------------------------------

def cli(*args: str, timeout: int = 90) -> tuple[bool, str]:
    """A mutating Vast CLI call. Never inherits stdin - see vast_state."""
    try:
        proc = subprocess.run(
            ["vastai", *args],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        out = f"{proc.stdout}\n{proc.stderr}".strip()
        return proc.returncode == 0 and "aborted" not in out.lower(), out
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def cli_json(*args: str, timeout: int = 90) -> Any:
    ok, out = cli(*args, "--raw", timeout=timeout)
    if not ok:
        return None
    try:
        return json.loads(out)
    except ValueError:
        # Some subcommands print a human line before the JSON body.
        start = min((i for i in (out.find("["), out.find("{")) if i >= 0),
                    default=-1)
        if start < 0:
            return None
        try:
            return json.loads(out[start:])
        except ValueError:
            return None


def search_params() -> str:
    params = ENV.get("VAST_SEARCH_PARAMS", "").strip()
    if not params:
        raise SystemExit("VAST_SEARCH_PARAMS missing from .env")
    return params


def template() -> dict:
    """The live template record. We read it rather than rebuilding it so the
    instances this module rents cannot drift from the ones renew_provisioning
    configures - one source of truth for the image, onstart and environment."""
    data = cli_json("search", "templates", f"creator_id={CREATOR_ID}")
    items = data.get("templates", data) if isinstance(data, dict) else data
    for t in items or []:
        if t.get("id") == TEMPLATE_ID:
            return t
    raise SystemExit(f"template {TEMPLATE_ID} not found for creator {CREATOR_ID}")


def rent_env(tmpl: dict) -> str:
    """The template's env, plus the two things it does not do for us.

    1. The port. The template maps 3000 (pyworker) and nothing else, because
       under the serverless model the router is supposed to be the only thing
       that talks to an instance. We talk to ComfyUI directly, so 18188 has to
       be reachable from outside, and a host that cannot publish it is a host
       we do not want.

    2. The bind address. ComfyUI defaults to ``--listen 127.0.0.1``, and the
       template never overrides it because the pyworker reaches ComfyUI over
       loopback. Publishing the port is then necessary but not sufficient:
       docker forwards 18188 to a socket nothing is listening on, and the
       probe times out on a machine that is otherwise perfectly healthy. This
       cost one wrongly vetoed host (32499) before it was found - provisioning
       had finished and /health was answering while 18188 refused.
    """
    env = (tmpl.get("env") or "").strip()
    if f"-p {COMFY_PORT}:{COMFY_PORT}" not in env:
        env = f"{env} -p {COMFY_PORT}:{COMFY_PORT}".strip()
    if "--listen" not in env:
        # COMFYUI_ARGS arrives quoted in the template env; extend the value in
        # place rather than appending a second -e that would be ignored.
        env = re.sub(
            r'(-e COMFYUI_ARGS=")([^"]*)(")',
            lambda m: f"{m.group(1)}{m.group(2)} --listen 0.0.0.0{m.group(3)}",
            env, count=1,
        )
    return env


# --- offers ------------------------------------------------------------------

def measured_seconds() -> dict[str, float]:
    """Median measured render seconds per GPU model, from our own run history.

    Built from the rows this fleet has already produced rather than from any
    vendor number. Empty on a fresh checkout, which is the correct answer then.
    """
    from statistics import median

    seen: dict[str, list[float]] = {}
    path = ROOT / "logs" / "fleet_loop.jsonl"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    for ln in lines:
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        gpu, lat = row.get("gpu"), row.get("latency")
        if not gpu or not isinstance(lat, (int, float)) or lat <= 0:
            continue
        seen.setdefault(str(gpu).strip(), []).append(float(lat))
    return {g: median(v) for g, v in seen.items()}


def offers() -> list[dict]:
    """Rentable offers under the ceiling, best first.

    Ranked by what the card measured on *this* graph, not by dlperf.

    This used to sort on dlperf descending, on the theory that under a fixed
    ceiling the useful question is "what is fastest that still fits". The
    theory is right and the proxy is wrong. dlperf ranked an RTX PRO 4000 at
    66 and an RTX 3090 at 44; on the served graph the PRO 4000 renders in
    ~104s and the 3090 in ~82s, so the metric bought the slower card at a
    higher price, twice. dlperf is a synthetic mixed-precision score and this
    workload is one long SDXL sampler plus an UltimateSDUpscale tile loop; the
    two do not correlate here.

    So: cards we have timed are ranked by their own median. A card we have
    never timed sorts immediately after the best known one, so an unmeasured
    offer gets tried - and measured - before we fall back to a card already
    known to be slow, but never ahead of our best. inet_down breaks ties
    because cold start is dominated by the model download, and price breaks
    what is left.
    """
    data = cli_json("search", "offers", search_params(), "-o", "dph_total")
    if not isinstance(data, list):
        return []
    known = measured_seconds()
    best = min(known.values()) if known else 0.0
    out = []
    for o in data:
        try:
            dph = float(o.get("dph_total"))
        except (TypeError, ValueError):
            continue
        if dph > DPH_CEILING + 1e-9:
            continue
        o["_dph"] = dph
        o["_inet"] = float(o.get("inet_down") or 0.0)
        name = str(o.get("gpu_name") or "").strip()
        o["_gpu"] = name
        if name in known:
            o["_cost"] = known[name]
            o["_new"] = 0
        else:
            # Just behind the best measured card: worth one trial, not a gamble
            # ahead of the only thing we have proven.
            o["_cost"] = best
            o["_new"] = 1
        out.append(o)
    out.sort(key=lambda o: (o["_cost"], o["_new"], -o["_inet"], o["_dph"]))
    return out


def veto(machine_id: Any, reason: str = "") -> bool:
    """Add a machine to the .env exclusion list so no later search offers it.

    The list is rebuilt from .env and never from the API's echo of it: the
    server appends its own ``verified=true``/``rented=false`` to whatever it
    stores, and round-tripping through that quietly restores filters that were
    removed on purpose.
    """
    mid = str(machine_id or "").strip()
    if not mid:
        return False
    envf = ROOT / ".env"
    try:
        lines = envf.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log(f"  could not read .env to veto {mid}: {exc}")
        return False
    key = "VAST_SEARCH_PARAMS="
    idx = next((i for i, ln in enumerate(lines) if ln.startswith(key)), None)
    if idx is None:
        return False
    params = lines[idx][len(key):].strip()
    head, sep, tail = params.partition("machine_id notin [")
    if not sep:
        params = f"{params} machine_id notin [{mid}]"
    else:
        current = [x.strip() for x in tail.split("]", 1)[0].split(",") if x.strip()]
        if mid in current:
            return True
        params = head + sep + ",".join(current + [mid]) + "]" + tail.split("]", 1)[1]
    lines[idx] = key + params
    envf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ENV["VAST_SEARCH_PARAMS"] = params
    log(f"  machine {mid} vetoed{(' - ' + reason) if reason else ''}")
    return True


# --- instances ---------------------------------------------------------------

def ours() -> list[dict]:
    """Every instance this module is responsible for.

    Matched by label rather than by the state file, so an instance orphaned by
    a crash between ``create`` and the first state write is still found and
    released instead of billing quietly forever.
    """
    return [i for i in vs.instances() if (i.get("label") or "") == LABEL]


def find(iid: Any) -> dict | None:
    iid = str(iid)
    for i in vs.instances():
        if str(i.get("id")) == iid:
            return i
    return None


def destroy(iid: Any, why: str = "") -> bool:
    ok, out = cli("destroy", "instance", str(iid), "-y", timeout=120)
    if ok:
        log(f"  instance {iid} destroyed{(' - ' + why) if why else ''}")
    else:
        log(f"  could not destroy {iid}: {out.strip()[:200]}")
    return ok


def over_ceiling(inst: dict) -> bool:
    try:
        dph = float(inst.get("dph_total"))
    except (TypeError, ValueError):
        return False
    return dph > DPH_CEILING + 1e-9


def comfy_url(inst: dict) -> str | None:
    """http://ip:port for ComfyUI, or None while the mapping is not published.

    Deliberately the instance's own address and not the shared cloudflared
    hostname. The named tunnel carries one token for every worker, so during a
    handover two instances answer on the same name and a request can land on
    the one that is being torn down. The tunnel stays useful for admin access;
    it has no business on the request path.
    """
    ip = inst.get("public_ipaddr")
    mapping = (inst.get("ports") or {}).get(COMFY_KEY) or []
    port = mapping[0].get("HostPort") if mapping else None
    if not ip or not port:
        return None
    return f"http://{str(ip).strip()}:{port}"


def http(url: str, payload: Any = None, timeout: float = PROBE_TIMEOUT,
         raw: bool = False) -> Any:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        # ComfyUI puts the whole reason a prompt was rejected in the error
        # body - `node_errors` names the node and the field it did not like.
        # urllib discards it, which is how a validation failure reaches the
        # page as a bare "HTTP Error 400: Bad Request" and looks like a
        # transport problem. Fold the body into the message; the exception
        # stays an HTTPError, so callers probing with URLError still catch it.
        try:
            detail = exc.read().decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001 - a body we cannot read is not the error
            detail = ""
        if detail:
            exc.msg = f"{exc.msg}: {detail[:800]}"
        raise
    if raw:
        return body
    return json.loads(body) if body else None


def comfy_ready(url: str) -> bool:
    """True once the server answers *and* our checkpoint is on disk.

    The two halves matter separately. ComfyUI binds its port as soon as it
    starts, which on a cold machine is minutes before provisioning has finished
    pulling weights; a worker accepted on the port alone fails its first render
    with a missing-model error, which is exactly what a wedged slot looks like
    from the outside.
    """
    try:
        info = http(f"{url}/object_info/CheckpointLoaderSimple", timeout=PROBE_TIMEOUT)
    except (urllib.error.URLError, OSError, ValueError):
        return False
    try:
        names = info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
    except (KeyError, IndexError, TypeError):
        return False
    return READY_CHECKPOINT in (names or [])


# --- state -------------------------------------------------------------------

def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def touch() -> None:
    """Record that a request just used the worker, so idle release backs off."""
    state = load_state()
    state["last_job"] = time.time()
    save_state(state)


def lease(iid: Any = "", ttl: float | None = None) -> None:
    """Claim the worker while a boot or a render is in flight.

    ``tick()`` finds instances by label rather than through the state file, so
    anything wearing our label the state file does not name is an orphan and
    gets destroyed. That is right after a crash and wrong during the minutes
    ``up()`` spends in ``wait_ready`` before it has a record worth writing: the
    daemon would destroy the worker the API rented thirty seconds ago. Until
    now nothing ran ``tick()`` on a timer, so the window never opened. It opens
    the moment the daemon and the API share a machine, which is the point of
    running them on a server at all.

    The lease is what tells the two apart - a marker with an expiry that says a
    live process is working on this. An empty ``iid`` covers the gap between
    ``create`` and knowing the id, and protects every instance for that tick.
    """
    LEASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEASE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "instance": str(iid or ""),
        "until": time.time() + (LEASE_TTL if ttl is None else ttl),
        "pid": os.getpid(),
    }), encoding="utf-8")
    tmp.replace(LEASE_PATH)


def leased() -> dict | None:
    """The live lease, or None. An expired file reads the same as no file."""
    try:
        held = json.loads(LEASE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if float(held.get("until") or 0) < time.time():
        return None
    return held


def lease_clear() -> None:
    """Drop the claim. Called when we release on purpose, so the next tick
    does not have to wait out the expiry to reconcile."""
    with suppress(OSError):
        LEASE_PATH.unlink()


# --- bring-up ----------------------------------------------------------------

def funds_short() -> float | None:
    """The balance if it can no longer pay for a rental, else None.

    Past the account's own threshold Vast answers ``create`` with a refusal per
    offer and no reason attached, so the loop walks the whole ranked pool in
    five seconds and reports "machine unavailable" five times. That looks
    exactly like a market problem and is not one. Checked once before renting
    rather than diagnosed afterwards. None on any doubt: a failure to read the
    account is not a reason to stop renting.
    """
    data = cli_json("show", "user", timeout=60)
    if not isinstance(data, dict):
        return None
    bal, floor = data.get("balance"), data.get("balance_threshold")
    try:
        bal = float(bal)
        floor = float(floor) if floor is not None else 0.0
    except (TypeError, ValueError):
        return None
    return bal if bal <= floor else None

def rent(tmpl: dict, skip: Iterable[Any] = ()) -> dict | None:
    """Take the best offer that actually accepts the booking.

    ``--cancel-unavail`` is the important flag: without it a machine that has
    been taken since the search returns a *stopped* instance instead of an
    error, which then sits in the account looking like a worker that failed to
    boot. With it the call fails cleanly and we move to the next offer, which
    is the "machine unavailable" case the whole loop has to survive.
    """
    skip = {str(s) for s in skip}
    pool = [o for o in offers() if str(o.get("machine_id")) not in skip]
    if not pool:
        log("  no offer under the ceiling")
        return None
    short = funds_short()
    if short is not None:
        raise SystemExit(
            f"account balance {short:.3f} is at or below the billing threshold; "
            "Vast refuses every booking until it is topped up. Nothing to debug "
            "in the loop - add credit and re-run."
        )
    env = rent_env(tmpl)
    for offer in pool[:6]:
        mid, oid = offer.get("machine_id"), offer.get("id")
        # Says which number decided this, so a bad pick is attributable:
        # either we had timed the card or we are spending a cycle to time it.
        rank = ("untimed" if offer.get("_new")
                else f"{offer.get('_cost', 0):.0f}s measured")
        log(f"  renting offer {oid} machine {mid}: {offer.get('gpu_name')} "
            f"({rank}) at {offer['_dph']:.4f}/h")
        data = cli_json(
            "create", "instance", str(oid),
            "--template_hash", str(tmpl.get("hash_id")),
            "--disk", str(DISK),
            "--env", env,
            "--label", LABEL,
            "--cancel-unavail",
            timeout=120,
        )
        iid = (data or {}).get("new_contract")
        if not data or not data.get("success", True) or not iid:
            why = (data or {}).get("msg") or (data or {}).get("error") or ""
            log(f"  offer {oid} refused{': ' + str(why)[:160] if why else ''}"
                f"; trying the next one")
            continue
        return {"instance": str(iid), "machine": str(mid),
                "dph": offer["_dph"], "gpu": offer.get("gpu_name"),
                # What we expected this card to cost, so the row that records
                # what it actually cost can be compared against the pick.
                "expected": offer.get("_cost"), "untimed": bool(offer.get("_new")),
                "rented_at": time.time()}
    log("  every offer refused the booking")
    return None


def wait_ready(rec: dict, deadline: float) -> str | None:
    """Poll until ComfyUI answers with our checkpoint loaded. URL or None.

    A machine that fails for a reason that belongs to the host is vetoed: it
    will fail the same way on the next attempt, and the serverless runs spent
    most of their wall clock rediscovering that about the same few machines.

    Running out of the cap is NOT such a reason. Provisioning clones five
    custom-node repos and pip-installs their requirements before it downloads a
    single model, and on a cold disk that legitimately outruns BOOT_CAP. The
    host is healthy and will be fine next time; only the wait was too short.
    Vetoing it writes a guess into .env permanently and burns the best card in
    the pool - that is how 32499 (RTX PRO 4000, the fastest offer we rank) got
    excluded twice. So the timeout only vetoes when the port never published,
    which is the case where we genuinely cannot use the host at all.

    ``exited`` is not believed on sight either. After an accepted ``start`` the
    API keeps reporting the instance's pre-start status for tens of seconds -
    measured at 7s, 33s and 46s on three restarts that all went on to serve. A
    caller that reads that first poll as death destroys a perfectly good
    machine and rents another; that is what turned one paused cycle into three
    rentals and 933s. A dead status therefore only counts once the instance has
    been seen alive, or once GRACE has passed, and only if it holds twice.
    """
    iid = rec["instance"]
    said_port = False
    entered = time.time()
    ever_alive = False
    dead_streak = 0
    while time.time() < deadline:
        # Held every pass, not once at entry: a boot legitimately runs past
        # BOOT_CAP on a cold disk, and the lease has to outlast the wait.
        lease(iid)
        inst = find(iid)
        if inst is None:
            log(f"  instance {iid} vanished before it was ready")
            return None
        if over_ceiling(inst):
            log(f"  instance {iid} priced at {inst.get('dph_total')}/h, over "
                f"the {DPH_CEILING:.3f} ceiling")
            veto(rec["machine"], "rented above the ceiling")
            destroy(iid, "over ceiling")
            return None
        status = (inst.get("actual_status") or inst.get("cur_state") or "").lower()
        if status in ("running", "loading", "created", "starting"):
            ever_alive = True
        if status in ("exited", "stopped"):
            dead_streak += 1
            if (ever_alive or time.time() - entered >= STATUS_GRACE) and dead_streak >= 2:
                log(f"  instance {iid} went {status} on its own")
                veto(rec["machine"], f"instance {status} during boot")
                destroy(iid, status)
                return None
        else:
            dead_streak = 0
        url = comfy_url(inst)
        if url and not said_port:
            said_port = True
            log(f"  port published, probing ComfyUI ({int(deadline - time.time())}s left)")
        if url and comfy_ready(url):
            return url
        time.sleep(POLL)
    log(f"  instance {iid} never became ready within {BOOT_CAP:.0f}s")
    if said_port:
        log("  port was serving, so the host works - not vetoing, just slow")
    else:
        veto(rec["machine"], "never published the ComfyUI port")
    destroy(iid, "boot timeout")
    return None


def up(attempts: int = 3, boot_cap: float | None = None) -> dict | None:
    """Ensure exactly one ready worker exists. Returns the state record.

    Covers the three cases the goal names, in this order: an instance we
    already own that is running (reuse it), one that is stopped (start it, do
    not pay to provision again), and nothing at all (rent). Anything wearing
    our label that is none of those is released, which is also how a run that
    died mid-cycle gets cleaned up.
    """
    cap = BOOT_CAP if boot_cap is None else boot_cap
    tmpl = template()
    state = load_state()
    skip: list[str] = []

    for attempt in range(1, attempts + 1):
        # Taken before the first look at the account, with no id yet: between
        # create and the state write there is nothing that names the instance,
        # so the blank lease has to cover whatever is wearing the label.
        lease("")
        fleet = ours()
        # Never keep two. The extras are the expensive kind of bug.
        for extra in fleet[1:]:
            destroy(extra.get("id"), "second instance under the same label")
        inst = fleet[0] if fleet else None

        if inst is not None and over_ceiling(inst):
            veto(inst.get("machine_id"), "existing instance over the ceiling")
            destroy(inst.get("id"), "over ceiling")
            inst = None

        if inst is not None:
            iid = str(inst.get("id"))
            status = (inst.get("actual_status") or inst.get("cur_state") or "").lower()
            rec = {"instance": iid,
                   "machine": str(inst.get("machine_id")),
                   "dph": inst.get("dph_total"),
                   "gpu": inst.get("gpu_name"),
                   "rented_at": state.get("rented_at", time.time())}
            if status == "running":
                url = comfy_url(inst)
                if url and comfy_ready(url):
                    log(f"  worker {iid} already serving on {inst.get('gpu_name')}")
                    rec.update(url=url, ready_at=time.time(), last_job=time.time())
                    save_state(rec)
                    return rec
                log(f"  worker {iid} running but not serving yet; waiting")
            elif status in ("stopped", "exited"):
                # The paused case. Starting costs seconds against the ten
                # minutes a fresh rental spends downloading the same models
                # onto the same disk that still has them.
                log(f"  worker {iid} is {status}; starting it again")
                ok, out = cli("start", "instance", iid, timeout=120)
                if not ok:
                    log(f"  could not start {iid}: {out.strip()[:200]}")
                    veto(inst.get("machine_id"), "refused to start")
                    destroy(iid, "start refused")
                    skip.append(str(inst.get("machine_id")))
                    continue
            else:
                log(f"  worker {iid} is {status or 'unknown'}; waiting on it")
            url = wait_ready(rec, time.time() + cap)
            if url:
                rec.update(url=url, ready_at=time.time(), last_job=time.time())
                save_state(rec)
                log(f"  worker ready at attempt {attempt} on {rec.get('gpu')}")
                return rec
            skip.append(rec["machine"])
            continue

        rec = rent(tmpl, skip=skip)
        if rec is None:
            return None
        save_state(rec)
        url = wait_ready(rec, time.time() + cap)
        if url:
            rec.update(url=url, ready_at=time.time(), last_job=time.time())
            save_state(rec)
            log(f"  worker ready in {rec['ready_at'] - rec['rented_at']:.0f}s "
                f"on {rec.get('gpu')} (machine {rec['machine']})")
            return rec
        skip.append(rec["machine"])
        save_state({})

    log(f"  gave up after {attempts} attempts")
    return None


def down(why: str = "requested") -> int:
    """Release everything we own and forget it."""
    n = 0
    # Dropped first: releasing on purpose outranks any claim, and leaving it
    # behind would have the next tick protect an instance that is already gone.
    lease_clear()
    for inst in ours():
        if destroy(inst.get("id"), why):
            n += 1
    save_state({})
    return n


# --- dispatch ----------------------------------------------------------------

def missing_inputs(url: str, workflow: dict, timeout: float = 20.0) -> list[str]:
    """The loader values in ``workflow`` that this worker cannot satisfy.

    ComfyUI validates every combo input against what it can actually see on
    disk, and rejects the whole prompt with a 400 when one does not match. That
    is the correct answer, but it arrives after the worker is rented and reads
    like a broken endpoint. Asking ``/object_info`` the same question first
    turns "HTTP Error 400: Bad Request" into the name of the missing file - and
    lets a caller wait for a model that is still downloading behind the ready
    signal instead of failing against it.

    Values arriving as a list are links to another node, not choices, and are
    skipped. An ``/object_info`` we cannot read is not evidence of anything, so
    it reports nothing missing and leaves the verdict to the server.
    """
    specs: dict[str, dict] = {}
    missing: list[str] = []
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        cls = node.get("class_type")
        if not cls:
            continue
        if cls not in specs:
            try:
                info = http(f"{url}/object_info/{cls}", timeout=timeout)
                specs[cls] = (info or {}).get(cls, {}).get(
                    "input", {}).get("required", {}) or {}
            except Exception:                              # noqa: BLE001
                specs[cls] = {}
        for field, value in (node.get("inputs") or {}).items():
            if isinstance(value, (list, dict)):
                continue
            spec = specs[cls].get(field)
            # A combo input is declared as [[option, ...], {...}].
            if isinstance(spec, list) and spec and isinstance(spec[0], list):
                if value not in spec[0]:
                    missing.append(f"{cls}.{field}={value}")
    return missing


def submit(url: str, workflow: dict, client_id: str = "fleet") -> str:
    """Queue a prompt. Returns ComfyUI's own prompt id.

    Note what is absent: there is no slot to reserve and nothing to release.
    If this process dies here the worker finishes the render and idles, and the
    next caller finds it ready - rather than finding a counter that says one
    request is in flight against a worker doing nothing.
    """
    res = http(f"{url}/prompt", {"prompt": workflow, "client_id": client_id},
               timeout=30)
    pid = (res or {}).get("prompt_id")
    if not pid:
        raise RuntimeError(f"ComfyUI refused the prompt: {res}")
    return str(pid)


def wait_job(url: str, prompt_id: str, timeout: float = 300.0) -> dict:
    """Block until the prompt leaves the history as finished. Raises on timeout.

    Holds the lease while it waits. This loop is the one place that reliably
    knows a render is still running, and a long one outlives IDLE_AFTER.
    """
    deadline = time.time() + timeout
    last_held = 0.0
    while time.time() < deadline:
        try:
            hist = http(f"{url}/history/{prompt_id}", timeout=15)
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(1.0)
            continue
        if time.time() - last_held > LEASE_TTL / 4:
            lease(load_state().get("instance") or "")
            last_held = time.time()
        entry = (hist or {}).get(prompt_id)
        if entry:
            status = (entry.get("status") or {})
            if status.get("completed") or status.get("status_str") == "success":
                return entry
            if status.get("status_str") == "error":
                raise RuntimeError(f"render failed: {status.get('messages')}")
        time.sleep(0.5)
    raise TimeoutError(f"prompt {prompt_id} unfinished after {timeout:.0f}s")


def images(url: str, entry: dict) -> list[tuple[str, bytes]]:
    """Every image the prompt saved, fetched straight off the worker.

    The serverless path uploaded these to R2 and handed back URLs, which added
    an upload and a download to a render measured in seconds. The bytes are
    already on the machine we are talking to.
    """
    out: list[tuple[str, bytes]] = []
    for node in (entry.get("outputs") or {}).values():
        for img in node.get("images") or []:
            if img.get("type") not in (None, "output"):
                continue
            q = urllib.parse.urlencode({
                "filename": img.get("filename", ""),
                "subfolder": img.get("subfolder", ""),
                "type": img.get("type", "output"),
            })
            out.append((img.get("filename", "image.png"),
                        http(f"{url}/view?{q}", timeout=60, raw=True)))
    return out


def interrupt(url: str) -> bool:
    """A cancel that actually stops the GPU, which the router version did not."""
    try:
        http(f"{url}/interrupt", payload={}, timeout=10)
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def render(workflow: dict, out_dir: Path | None = None,
           timeout: float = 300.0) -> dict:
    """Bring a worker up if needed, render, save the images, report timings."""
    rec = load_state()
    url = rec.get("url")
    if not url or not comfy_ready(url):
        rec = up() or {}
        url = rec.get("url")
    if not url:
        return {"ok": False, "error": "no worker"}
    started = time.time()
    pid = submit(url, workflow)
    entry = wait_job(url, pid, timeout=timeout)
    latency = time.time() - started
    saved = []
    for name, blob in images(url, entry):
        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / name).write_bytes(blob)
            saved.append(str(out_dir / name))
        else:
            saved.append(name)
    touch()
    return {"ok": True, "latency": latency, "prompt_id": pid, "images": saved}


# --- control loop ------------------------------------------------------------

def tick() -> dict:
    """One control step. Safe to call on a timer forever.

    Reconciliation is one-directional on purpose: the account is the truth, the
    state file is a cache. Anything labelled ours that the state file does not
    know about gets destroyed, because the only way for that to happen is a
    crash mid-rental, and the alternative to destroying it is paying for it.

    The one other way for that to happen is a rental still in progress, which
    is not a crash and must not be destroyed. A live lease is the difference;
    an expired one is not, which is what keeps a dead API from parking a GPU.
    """
    state = load_state()
    fleet = ours()
    known = str(state.get("instance") or "")
    hold = leased()

    for inst in fleet:
        iid = str(inst.get("id"))
        if iid != known:
            if hold is not None and hold.get("instance") in ("", iid):
                log(f"  instance {iid} is leased by pid {hold.get('pid')}; "
                    f"leaving it alone")
                continue
            destroy(iid, "not in the fleet state")
        elif over_ceiling(inst):
            veto(inst.get("machine_id"), "priced over the ceiling while running")
            destroy(iid, "over ceiling")
            save_state({})

    if known and not any(str(i.get("id")) == known for i in fleet):
        log(f"  instance {known} is gone; clearing state")
        save_state({})
        return {"worker": None}

    state = load_state()
    if state.get("ready_at"):
        # A render that outruns IDLE_AFTER is not idle, and touch() only fires
        # on either side of the submit. Without this the reaper reaps the job
        # it is meant to be protecting, halfway through.
        if hold is not None:
            return {"worker": state.get("instance"), "url": state.get("url"),
                    "leased": True}
        idle = time.time() - float(state.get("last_job") or state["ready_at"])
        if idle > IDLE_AFTER:
            log(f"  idle for {idle:.0f}s; releasing the worker")
            down("idle")
            return {"worker": None, "released": True}
    return {"worker": state.get("instance"), "url": state.get("url")}


def daemon(interval: float = 30.0) -> int:
    log(f"fleet daemon up: ceiling {DPH_CEILING:.3f}/h, boot cap {BOOT_CAP:.0f}s, "
        f"idle release {IDLE_AFTER:.0f}s")
    while True:
        try:
            tick()
        except KeyboardInterrupt:
            log("daemon stopped")
            return 0
        except Exception as exc:  # noqa: BLE001
            log(f"tick failed: {type(exc).__name__}: {exc}")
        time.sleep(interval)


# --- cli ---------------------------------------------------------------------

def cmd_status() -> int:
    state = load_state()
    fleet = ours()
    if not fleet:
        print("no worker")
    for inst in fleet:
        url = comfy_url(inst)
        ready = comfy_ready(url) if url else False
        print(f"instance {inst.get('id')} machine {inst.get('machine_id')} "
              f"{inst.get('gpu_name')} {inst.get('dph_total')}/h "
              f"{inst.get('actual_status')} "
              f"{'serving' if ready else 'not serving'}")
        print(f"  comfy: {url or 'port not published'}")
    if state.get("ready_at"):
        print(f"  idle: {time.time() - float(state.get('last_job') or 0):.0f}s")
    pool = offers()
    print(f"{len(pool)} offers under {DPH_CEILING:.3f}/h")
    for o in pool[:5]:
        rank = "untimed" if o.get("_new") else f"{o.get('_cost', 0):.0f}s"
        print(f"  {str(o.get('gpu_name')):<16} {rank:>8} "
              f"{o['_dph']:.4f}/h  machine {o.get('machine_id')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p_up = sub.add_parser("up")
    p_up.add_argument("--attempts", type=int, default=3)
    p_down = sub.add_parser("down")
    p_down.add_argument("--why", default="requested")
    p_run = sub.add_parser("run")
    p_run.add_argument("workflow", type=Path)
    p_run.add_argument("--out", type=Path, default=ROOT / "output" / "fleet")
    p_run.add_argument("--timeout", type=float, default=300.0)
    sub.add_parser("tick")
    p_d = sub.add_parser("daemon")
    p_d.add_argument("--interval", type=float, default=30.0)
    args = ap.parse_args()

    if args.cmd == "status":
        return cmd_status()
    if args.cmd == "up":
        return 0 if up(attempts=args.attempts) else 1
    if args.cmd == "down":
        print(f"{down(args.why)} instance(s) released")
        return 0
    if args.cmd == "run":
        wf = json.loads(args.workflow.read_text(encoding="utf-8"))
        res = render(wf, out_dir=args.out, timeout=args.timeout)
        if not res.get("ok"):
            print(f"FAIL: {res.get('error')}", file=sys.stderr)
            return 1
        print(f"{len(res['images'])} image(s) in {res['latency']:.2f}s")
        for p in res["images"]:
            print(f"  {p}")
        return 0
    if args.cmd == "tick":
        print(json.dumps(tick(), indent=2))
        return 0
    if args.cmd == "daemon":
        return daemon(args.interval)
    return 2


if __name__ == "__main__":
    sys.exit(main())
