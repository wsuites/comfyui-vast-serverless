#!/usr/bin/env python3
"""Exercise every recovery case unstick() has to handle, without renting a GPU.

deploy_loop.py measures the real thing and costs money and an hour. This
covers the same decision table in a second, by standing in for the Vast CLI:
the cases are the ones the deployment actually hits - a machine that will not
give the instance back, an instance that is merely stopped, nothing rented at
all, and a container that is up with a worker that never finished booting.

Each case asserts on the *decision* (destroy / start / leave alone), because
that is what is expensive to get wrong. A false destroy throws away a warm
worker and buys a 7 minute cold start; a missed one leaves the endpoint dead
until somebody looks at it.

    python scripts/test_recovery.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import vast_state as vs  # noqa: E402


class Vast:
    """Stand-in for the CLI. Records what was asked of the real hardware."""

    def __init__(self, insts: list[dict], works: list[dict],
                 start: tuple[bool, str] = (True, "starting instance"),
                 offers: int | None = 0):
        self.insts, self.works = insts, works
        self.start_reply, self.offers = start, offers
        self.calls: list[str] = []

    def raw(self, *args: str, timeout: int = 60) -> tuple[bool, str]:
        self.calls.append(" ".join(args))
        if args[0] == "start":
            return self.start_reply
        if args[0] == "destroy":
            return True, "destroying instance"
        return True, ""

    def install(self) -> None:
        vs.instances = lambda: list(self.insts)
        vs.workers = lambda: list(self.works)
        vs._vastai_raw = self.raw
        vs.free_offers = lambda machine_id: self.offers

    # -- what the harness asks afterwards ---------------------------------
    @property
    def destroyed(self) -> bool:
        return any(c.startswith("destroy") for c in self.calls)

    @property
    def started(self) -> bool:
        return any(c.startswith("start") for c in self.calls)


def inst(status: str = "running", *, iid: int = 52101655,
         machine: int = 146299, age_min: float = 5.0,
         next_state: str | None = None) -> dict:
    return {"id": iid, "machine_id": machine, "actual_status": status,
            "next_state": next_state,
            "start_date": time.time() - age_min * 60}


def worker(*, ready_ever: bool = True, reqs_working: int = 0,
           perf: float = 39.9) -> dict:
    return {"id": 52101655, "ready_ever": ready_ever,
            "reqs_working": reqs_working, "measured_perf": perf,
            "started_at": time.time() - 300}


# ---------------------------------------------------------------- cases --
CASES: list[tuple[str, Vast, dict]] = []


def case(name: str, v: Vast, **expect) -> None:
    CASES.append((name, v, expect))


# 1. Nothing rented. The autoscaler's job; touching anything here would race
#    it into renting two machines for one endpoint.
case("nothing rented -> leave it to the autoscaler",
     Vast(insts=[], works=[]),
     acted=False, destroyed=False, detail="autoscaler")

# 2. Stopped, and the host will take it back. The probe *is* the fix: the
#    start it sends to find out is the same start that brings it up.
case("stopped on a host with room -> started, not replaced",
     Vast(insts=[inst("stopped")], works=[worker(ready_ever=False)],
          start=(True, "starting instance 52101655"), offers=3),
     acted=False, started=True, destroyed=False)

# 3. Stopped on a machine that has since filled up. This is the trap: the
#    instance record looks recoverable forever, and the autoscaler will not
#    rent a replacement while it exists. Only a destroy frees the endpoint.
case("stopped, machine full -> destroyed so the autoscaler re-rents",
     Vast(insts=[inst("stopped")], works=[worker(ready_ever=False)],
          start=(False, "required resources are currently unavailable"),
          offers=0),
     acted=True, destroyed=True, detail="unavailable")

# 4. Same shape, the other phrasing Vast uses when the offer is gone.
case("stopped, offer gone -> destroyed",
     Vast(insts=[inst("exited")], works=[],
          start=(False, "the instance is no longer available"), offers=0),
     acted=True, destroyed=True)

# 5. The start failed for some unrelated reason. Refusing to guess is the
#    point: a destroy on a transient API wobble costs a full cold start.
case("start fails for an unknown reason -> nothing destroyed on a guess",
     Vast(insts=[inst("stopped")], works=[],
          start=(False, "error 502 bad gateway"), offers=5),
     acted=False, destroyed=False),

# 6. Running and serving. Never touch it, whatever else the record says.
case("worker serving -> untouched",
     Vast(insts=[inst("running")], works=[worker(reqs_working=1)]),
     acted=False, destroyed=False, started=False, detail="serving")

# 7. Booting, inside the budget. A cold start is 4-7 min; this is minute 5.
case("booting inside the cap -> left to finish",
     Vast(insts=[inst("running", age_min=5)],
          works=[worker(ready_ever=False, perf=0)]),
     acted=False, destroyed=False)

# 8. Booting, past the cap. The hole instance 52098270 fell through: the
#    container is up and honest about it, the benchmark never returns, and
#    nothing else in the record can tell that apart from a slow download.
case("running past BOOT_CAP with no benchmark -> destroyed",
     Vast(insts=[inst("running", age_min=vs.BOOT_CAP / 60 + 2)],
          works=[worker(ready_ever=False, perf=0)]),
     acted=True, destroyed=True, detail="never became")

# 9. On its way up. next_state says running, so it is slow, not stranded.
case("starting (next_state running) -> not stranded",
     Vast(insts=[inst("stopped", next_state="running")], works=[]),
     acted=False, destroyed=False, started=False)

# 10. A dry run must never spend money, including the start probe, which
#     rents the machine as a side effect of asking.
case("dry run on a full machine -> reports, spends nothing",
     Vast(insts=[inst("stopped")], works=[],
          start=(False, "required resources are currently unavailable"),
          offers=0),
     dry_run=True, acted=True, destroyed=False, started=False)


# ----------------------------------------------------------------- run --
def main() -> int:
    saved = (vs.instances, vs.workers, vs._vastai_raw, vs.free_offers)
    failed = 0
    for name, v, expect in CASES:
        v.install()
        dry = bool(expect.pop("dry_run", False))
        try:
            report = vs.unstick(dry_run=dry)
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {name}\n       raised {type(exc).__name__}: {exc}")
            failed += 1
            continue
        # ``reason`` carries why a replacement happened, ``detail`` what the
        # CLI said about it; a case may name either.
        got = {"acted": bool(report.get("acted")),
               "destroyed": v.destroyed, "started": v.started,
               "detail": f"{report.get('reason') or ''} "
                         f"{report.get('detail') or ''}".strip()}
        bad = []
        for key, want in expect.items():
            if key == "detail":
                if want.lower() not in got["detail"].lower():
                    bad.append(f"detail {got['detail']!r} lacks {want!r}")
            elif got[key] != want:
                bad.append(f"{key}={got[key]}, wanted {want}")
        if bad:
            failed += 1
            print(f"FAIL {name}")
            for b in bad:
                print(f"       {b}")
        else:
            print(f"ok   {name}")
    vs.instances, vs.workers, vs._vastai_raw, vs.free_offers = saved
    print(f"\n{len(CASES) - failed}/{len(CASES)} recovery cases passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
