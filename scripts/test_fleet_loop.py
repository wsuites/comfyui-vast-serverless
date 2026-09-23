#!/usr/bin/env python3
"""Run the whole five-cycle deployment offline, against a fake Vast and a fake
ComfyUI, so run17 is not the first time this code path executes.

test_recovery.py covers unstick()'s decision table. This covers the layer above
it: the loop that rents, boots, renders, stops, restarts and releases. Those are
the six things the goal asks for, and every one of them costs a GPU-hour to try
for real - which is why the bugs in them survived so long. Three of the worst
were only ever reproduced live:

  * an accepted ``start`` whose status still reads ``exited`` for ~46s, which
    the loop read as a dead container and answered with two extra rentals,
  * an offer that refuses the booking while the rest of the pool is fine,
  * a byte-identical resubmit served out of ComfyUI's cache in 0.72s, which
    made warm renders look four seconds fast against a thirteen second target.

All three are modelled here. The fake ComfyUI charges per node, so the graph
without the upscale tail really does finish sooner and ``upscale_cost`` is
measured rather than assumed, and it records any prompt it has already seen -
so if the seeding regresses this suite fails, instead of quietly reporting a
fast lie the way the live runs did.

    python scripts/test_fleet_loop.py
"""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

CHECKPOINT = "waiIllustriousSDXL_v170.safetensors"
PNG = bytes.fromhex("89504e470d0a1a0a") + b"fake-png-body"


def fleet_label() -> str:
    """Whatever label the code under test is actually looking for.

    Hardcoding it meant a rename made every instance the fake mints invisible
    to ``ours()`` - which is the exact production failure a rename causes, and
    the suite would have reported it as "nothing rented" rather than as a
    label mismatch. Read late: ``install()`` may have moved it.
    """
    import fleet
    return fleet.LABEL

# What the fake GPU charges, per node class, scaled down 200x from the live
# measurement on a PRO 4000 (full graph 101.87s, without the upscale 20.06s,
# without upscale or face pass 11.39s). The shape matters more than the scale:
# a flat per-node cost makes the two graphs indistinguishable and the
# upscale_cost assertion cannot tell instrumentation from noise.
NODE_COST = {
    "UltimateSDUpscale": 81.8 / 200,
    "FaceDetailer": 8.7 / 200,
    "KSamplerAdvanced": 11.4 / 200,
    "KSampler": 11.4 / 200,
}
NODE_COST_DEFAULT = 0.05 / 200


def graph_seconds(graph):
    return sum(NODE_COST.get(n.get("class_type"), NODE_COST_DEFAULT)
               for n in graph.values())


class Comfy(BaseHTTPRequestHandler):
    """Enough of ComfyUI's HTTP API for fleet.py to believe it, including the
    behaviour that cost the most to discover: identical prompts are cached."""

    ready_at = 0.0              # models are not resident before this
    seen: dict[str, str] = {}   # prompt fingerprint -> prompt id
    done: dict[str, dict] = {}
    cache_hits = 0
    interrupts = 0
    lock = threading.Lock()

    def log_message(self, *a):  # silence the access log
        pass

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode())

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/object_info"):
            # Before the download finishes the node exists but the list is
            # empty. That is the exact shape that made a half-provisioned box
            # look ready, so the probe has to see the checkpoint by name.
            names = [CHECKPOINT] if time.time() >= Comfy.ready_at else []
            return self._json({"CheckpointLoaderSimple":
                               {"input": {"required": {"ckpt_name": [names]}}}})
        if path.startswith("/history/"):
            pid = path.rsplit("/", 1)[-1]
            rec = Comfy.done.get(pid)
            if rec is None or time.time() < rec["at"]:
                return self._json({})
            return self._json({pid: {
                "status": {"completed": True, "status_str": "success"},
                "outputs": {"9": {"images": [
                    {"filename": pid + ".png", "subfolder": "",
                     "type": "output"}]}},
            }})
        if path.startswith("/view"):
            return self._send(200, PNG, "image/png")
        return self._json({}, 404)

    def do_POST(self):
        # Counted, not just accepted: the point of owning the dispatch is that
        # a cancel reaches the sampler, and only a count can show it did.
        if self.path.startswith("/interrupt"):
            with Comfy.lock:
                Comfy.interrupts += 1
            return self._json({})
        if not self.path.startswith("/prompt"):
            return self._json({}, 404)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        graph = body.get("prompt") or {}
        fingerprint = json.dumps(graph, sort_keys=True)
        with Comfy.lock:
            if fingerprint in Comfy.seen:
                # Real ComfyUI answers this out of cache, near-instantly, and
                # the harness records it as a very fast render.
                Comfy.cache_hits += 1
                pid = Comfy.seen[fingerprint]
                Comfy.done[pid] = {"at": time.time()}
                return self._json({"prompt_id": pid})
            pid = "p" + str(len(Comfy.seen) + 1)
            Comfy.seen[fingerprint] = pid
            Comfy.done[pid] = {"at": time.time() + graph_seconds(graph)}
        return self._json({"prompt_id": pid})


class Vast:
    """A Vast account with one machine that refuses and the rest that work."""

    BOOT = 0.35       # create -> the port answers
    PROVISION = 0.5   # port -> the checkpoint is resident
    STALE = 0.30      # how long a started instance reports its old status

    def __init__(self, port, refuse_machine="999001"):
        self.port = port
        self.refuse = refuse_machine
        self.insts = {}
        self.next_id = 52300000
        self.calls = []
        self.rentals = 0

    def offers(self):
        return [
            # This one refuses, and it is deliberately the offer the ranking
            # likes best - same GPU model as the rest, most bandwidth. A
            # refusing machine the scheduler would never have picked proves
            # nothing about the walk to the next offer.
            {"id": 900001, "machine_id": int(self.refuse), "dph_total": 0.1999,
             "dlperf": 44.0, "inet_down": 2400.0, "gpu_name": "RTX 3090"},
            {"id": 900002, "machine_id": 136980, "dph_total": 0.2009,
             "dlperf": 44.0, "inet_down": 1872.0, "gpu_name": "RTX 3090"},
            {"id": 900003, "machine_id": 108820, "dph_total": 0.1481,
             "dlperf": 44.0, "inet_down": 1218.0, "gpu_name": "RTX 3090"},
            # An untimed card: must be tried, but never ahead of a timed one.
            {"id": 900004, "machine_id": 111222, "dph_total": 0.2100,
             "dlperf": 99.0, "inet_down": 3000.0, "gpu_name": "RTX 4090"},
        ]

    def instances(self):
        now = time.time()
        out = []
        for rec in self.insts.values():
            i = dict(rec)
            booted = now >= i["_boot_at"]
            # The quirk: for STALE seconds after a transition the API still
            # reports what the instance was doing before it.
            if now < i.get("_stale_until", 0):
                i["actual_status"] = i["_stale_status"]
            elif i["_target"] == "running":
                i["actual_status"] = "running" if booted else "loading"
            else:
                i["actual_status"] = i["_target"]
            live = i["actual_status"] == "running" and booted
            i["ports"] = ({"18188/tcp": [{"HostPort": str(self.port)}]}
                          if live else {})
            i["public_ipaddr"] = "127.0.0.1"
            for k in [k for k in i if k.startswith("_")]:
                i.pop(k)
            out.append(i)
        return out

    def cli(self, *args, timeout=90):
        a = [str(x) for x in args if str(x) != "--raw"]
        self.calls.append(" ".join(a))

        if a[:2] == ["show", "user"]:
            return True, json.dumps({"balance": 12.50, "credit": 12.50,
                                     "balance_threshold": -0.01})
        if a[:2] == ["search", "offers"]:
            return True, json.dumps(self.offers())
        if a[:2] == ["show", "instances"]:
            return True, json.dumps(self.instances())
        if a[0] == "create":
            offer = next((o for o in self.offers() if str(o["id"]) == a[2]), None)
            if offer is None:
                return True, json.dumps({"success": False, "msg": "no such offer"})
            if str(offer["machine_id"]) == self.refuse:
                return True, json.dumps({"success": False,
                                         "msg": "machine is unavailable"})
            iid = str(self.next_id)
            self.next_id += 1
            self.rentals += 1
            now = time.time()
            self.insts[iid] = {
                "id": int(iid), "machine_id": offer["machine_id"],
                "label": fleet_label(), "gpu_name": offer["gpu_name"],
                "dph_total": offer["dph_total"], "start_date": now,
                "_boot_at": now + self.BOOT, "_target": "running",
                "_stale_until": 0.0, "_stale_status": "created",
            }
            Comfy.ready_at = now + self.BOOT + self.PROVISION
            return True, json.dumps({"success": True, "new_contract": int(iid)})
        if a[0] == "destroy":
            self.insts.pop(a[2], None)
            return True, "destroying instance"
        if a[0] == "stop":
            rec = self.insts.get(a[2])
            if rec:
                rec["_target"] = "exited"
                rec["_stale_until"] = 0.0
            return True, "stopping instance"
        if a[0] == "start":
            rec = self.insts.get(a[2])
            if not rec:
                return False, "no such instance"
            now = time.time()
            rec["_target"] = "running"
            rec["_boot_at"] = now + self.BOOT
            # Accepted - but the API lies about it for a while.
            rec["_stale_until"] = now + self.STALE
            rec["_stale_status"] = "exited"
            Comfy.ready_at = now + self.BOOT  # the models survived the stop
            return True, "starting instance"
        return True, ""


def install(v):
    import fleet
    import vast_state as vs

    fleet.cli = v.cli
    fleet.cli_json = lambda *a, **k: json.loads(v.cli(*a, **k)[1] or "null")
    fleet.template = lambda: {"id": 549464, "image": "fake/comfy:latest",
                              "env": '-p 18188:18188 -e COMFYUI_ARGS="--fast"',
                              "onstart": "echo hi", "disk_space": 30}
    fleet.veto = lambda m, r="": False    # never poison .env from a test
    fleet.save_state = lambda s: None
    # Same reason as veto: the lease is a real file under logs/, and a suite
    # that leaves one behind hands the next live tick a claim on an instance
    # that never existed. test_lease.py covers the lease on its own.
    fleet.lease = lambda *a, **k: None
    fleet.leased = lambda: None
    fleet.lease_clear = lambda: None
    # Ranking reads logs/fleet_loop.jsonl through measured_seconds(), and that
    # file is whatever the developer's own live runs left behind: full of 3090
    # rows on the machine this was written on, absent on a fresh clone. With it
    # empty every card is untimed, the tie falls to inet_down, and the fake
    # 4090 wins - so the two ranking assertions below passed here and failed on
    # a clean checkout. That is exactly backwards for the suite whose job is to
    # gate the live runs that cost money.
    #
    # So the history is stated rather than inherited. 82s is the 3090's
    # measured median on the served graph; the fake 4090 stays untimed, which
    # is the case the ranking actually has to get right.
    fleet.measured_seconds = lambda: {"RTX 3090": 82.0}
    fleet.load_state = lambda: ({} if not v.insts
                                else {"instance": list(v.insts)[-1]})
    vs.instances = v.instances
    # Real-time constants scaled to the fake hardware.
    fleet.POLL = 0.05
    fleet.STATUS_GRACE = 0.6
    fleet.BOOT_CAP = 20.0
    fleet.PROBE_TIMEOUT = 3.0
    return fleet


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Comfy)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    v = Vast(port)
    fleet = install(v)
    import fleet_loop as fl

    fl.fleet = fleet
    fl.BOOT_TARGET = 20.0

    workflow = fl.workload()
    base = fl.workload(no_upscale=True)
    rows = []
    for i, kind in enumerate(fl.PLAN, 1):
        for attempt in range(1, 4):
            row = fl.cycle(i, kind, workflow, base)
            row["attempt"] = attempt
            if row.get("result") == "retry" and attempt < 3:
                continue
            break
        rows.append(row)
    fleet.down("test finished")
    srv.shutdown()

    print()
    print("%2s  %-7s %-7s %7s %7s %7s %-14s %s"
          % ("#", "cycle", "result", "boot", "lat", "base", "gpu", "note"))
    for r in rows:
        print("%2s  %-7s %-7s %7s %7s %7s %-14s %s"
              % (r["cycle"], r.get("kind", ""), r.get("result", "?"),
                 r.get("boot", "-"), r.get("latency", "-"),
                 r.get("base_latency", "-"), r.get("gpu", "-"),
                 r.get("error", "")))

    fails = []
    passed = sum(1 for r in rows if r.get("result") == "pass")
    if passed != len(fl.PLAN):
        fails.append("%d/%d cycles passed" % (passed, len(fl.PLAN)))
    if Comfy.cache_hits:
        fails.append("%d render(s) served from cache - seeding regressed and "
                     "the latency numbers are fiction" % Comfy.cache_hits)
    # Every cycle must produce both measurements, and the upscale tail must
    # cost something, or the instrumentation is not measuring anything.
    for r in rows:
        if r.get("result") != "pass":
            continue
        if r.get("base_latency") is None:
            fails.append("cycle %s: no base render" % r["cycle"])
        elif r.get("upscale_cost", 0) <= 0:
            fails.append("cycle %s: upscale tail measured %ss, so the two "
                         "graphs are identical" % (r["cycle"], r["upscale_cost"]))
    if not v.rentals:
        fails.append("nothing was ever rented")
    restarts = sum(1 for c in v.calls if c.startswith("start instance"))
    if not restarts:
        fails.append("the paused cycle never restarted an instance")
    paused = next((r for r in rows if r.get("kind") == "paused"), None)
    if paused and paused.get("result") == "pass" and paused.get("boot", 99) > 5:
        fails.append("paused cycle took %ss - the stale status window is being "
                     "read as a dead container again" % paused["boot"])
    if v.insts:
        fails.append("%d instance(s) left billing" % len(v.insts))
    # The refusing machine must be walked past once per rental, not retried
    # until the pool is exhausted.
    refused = sum(1 for c in v.calls if c.startswith("create") and "900001" in c)
    if not refused:
        fails.append("the unavailable machine was never offered, so walking "
                     "past it is untested")
    if refused > v.rentals + len(fl.PLAN):
        fails.append("the unavailable machine was retried %d times" % refused)
    # The untimed card must never outrank a card we have already measured.
    first_picks = [c for c in v.calls if c.startswith("create")]
    if any("900004" in c for c in first_picks[:1]):
        fails.append("an untimed card was picked ahead of a measured one")

    print()
    print("rentals: %d   restarts: %d   refusals walked: %d   cache hits: %d"
          % (v.rentals, restarts, refused, Comfy.cache_hits))
    for f in fails:
        print("FAIL " + f)
    print()
    print("all five cycles deployed clean" if not fails else "FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
