#!/usr/bin/env python3
"""The lease decision table, without renting a GPU.

``tick()`` finds instances by label and destroys anything the state file does
not name. That is correct after a crash and catastrophic during a rental still
in progress, and until the daemon ran on a timer next to the API the two could
not happen on the same machine, so the window never opened. Moving the
autoscaler onto a server opens it: ``up()`` spends minutes in ``wait_ready``
before it has a record worth writing, and a ``tick()`` landing in that window
destroys the worker the API rented thirty seconds earlier.

The lease is the difference between a rental in progress and an orphan. The
cases that matter are not "does it hold" but the two either side of it:

  * an expired lease must NOT protect anything - that is the crashed-API case,
    and believing a stale claim buys exactly the overnight bill the idle
    reaper exists to prevent,
  * a lease naming one instance must not shelter a different one, or a real
    orphan rides in on a live rental's claim.

    python scripts/test_lease.py
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import fleet  # noqa: E402
import vast_state as vs  # noqa: E402


class Harness:
    """State and lease on disk in a temp dir; destroy() recorded, not done."""

    def __init__(self, tmp: Path):
        self.destroyed: list[str] = []
        self.released = False
        fleet.STATE_PATH = tmp / "fleet.json"
        fleet.LEASE_PATH = tmp / "fleet.lease"
        fleet.LEASE_TTL = 120.0
        fleet.IDLE_AFTER = 600.0
        fleet.DESTROY_AFTER = 3600.0
        fleet.destroy = self._destroy
        fleet.pause = self._pause
        fleet.over_ceiling = lambda inst: False
        # Both accessors, and the binary under them. Production reads the
        # fleet through ``instances_strict``; a stub that only covers the
        # lenient alias does not fake the account, it falls through to the
        # real one - which is how this suite spent a run listing live
        # hardware and deciding a rented instance was not in its state file.
        vs.instances = lambda: self.insts
        vs.instances_strict = lambda: self.insts
        vs.vastai_bin = lambda: ""
        self.insts: list[dict] = []
        self.stopped: list[str] = []

    def _destroy(self, iid, why: str = "") -> bool:
        self.destroyed.append(str(iid))
        return True

    def _pause(self, iid, why: str = "") -> bool:
        self.stopped.append(str(iid))
        return True

    def account(self, *ids: str) -> None:
        """Everything on the account, all wearing our label."""
        self.insts = [{"id": i, "label": fleet.LABEL, "actual_status": "running",
                       "machine_id": "999", "dph_total": 0.20,
                       "gpu_name": "RTX 3090"} for i in ids]

    def state(self, **kw) -> None:
        fleet.save_state(kw)

    def clear(self) -> None:
        self.destroyed = []
        self.stopped = []
        fleet.lease_clear()
        fleet.save_state({})


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="lease-"))
    h = Harness(tmp)
    fails: list[str] = []

    def check(name: str, want_destroyed: list[str], want_released: bool = False,
              result: dict | None = None, want_stopped: list[str] = []) -> None:
        got = sorted(h.destroyed)
        stopped = sorted(h.stopped)
        ok = got == sorted(want_destroyed) and stopped == sorted(want_stopped)
        released = bool((result or {}).get("released"))
        ok = ok and released == want_released
        print(f"{'ok  ' if ok else 'FAIL'} {name}")
        if not ok:
            fails.append(name)
            print(f"       destroyed={got} want={sorted(want_destroyed)} "
                  f"stopped={stopped} want={sorted(want_stopped)} "
                  f"released={released} want={want_released}")

    # --- the orphan path, which must keep working -----------------------------
    h.clear()
    h.account("52300001")
    h.state()
    r = fleet.tick()
    check("no lease, instance the state file does not name -> destroyed",
          ["52300001"], result=r)

    # --- a rental in progress -------------------------------------------------
    h.clear()
    h.account("52300001")
    h.state()
    fleet.lease("")                       # created, id not yet written
    r = fleet.tick()
    check("blank lease, id not written yet -> survives", [], result=r)

    h.clear()
    h.account("52300001")
    h.state()
    fleet.lease("52300001")
    r = fleet.tick()
    check("lease naming the instance -> survives", [], result=r)

    # --- a lease must not shelter its neighbours ------------------------------
    h.clear()
    h.account("52300001", "52300002")
    h.state()
    fleet.lease("52300001")
    r = fleet.tick()
    check("lease naming one instance -> the other is still an orphan",
          ["52300002"], result=r)

    # --- the crashed-API case, which is the whole point of the expiry ---------
    h.clear()
    h.account("52300001")
    h.state()
    fleet.lease("52300001", ttl=-1)       # held by a process that then died
    r = fleet.tick()
    check("expired lease -> destroyed, not believed", ["52300001"], result=r)

    # --- the idle reaper ------------------------------------------------------
    h.clear()
    h.account("52300001")
    h.state(instance="52300001", url="http://x", ready_at=time.time() - 5000,
            last_job=time.time() - 5000)
    r = fleet.tick()
    check("idle past IDLE_AFTER, no lease -> stopped, not destroyed", [],
          want_stopped=["52300001"], result=r)
    stopped_state = fleet.load_state()
    ok = (stopped_state.get("stopped_at") and not stopped_state.get("url")
          and stopped_state.get("instance") == "52300001")
    print(f"{'ok  ' if ok else 'FAIL'} the stop leaves a record `up` can start "
          f"again")
    if not ok:
        fails.append("stop leaves a startable record")

    # The second stage. The GPU is already off; this is the disk bill ending.
    h.clear()
    h.account("52300001")
    h.state(instance="52300001", stopped_at=time.time() - 5000)
    r = fleet.tick()
    check("stopped past DESTROY_AFTER -> destroyed", ["52300001"],
          want_released=True, result=r)

    h.clear()
    h.account("52300001")
    h.state(instance="52300001", stopped_at=time.time() - 60)
    r = fleet.tick()
    check("stopped inside the window -> kept, disk and all", [], result=r)

    h.clear()
    h.account("52300001")
    h.state(instance="52300001", url="http://x", ready_at=time.time() - 5000,
            last_job=time.time() - 5000)
    fleet.lease("52300001")
    r = fleet.tick()
    check("a render outrunning IDLE_AFTER -> not reaped mid-job", [],
          result=r)

    h.clear()
    h.account("52300001")
    h.state(instance="52300001", url="http://x", ready_at=time.time() - 5000,
            last_job=time.time() - 5000)
    fleet.lease("52300001", ttl=-1)
    r = fleet.tick()
    check("render lease expired with the process -> stopped", [],
          want_stopped=["52300001"], result=r)

    # --- releasing on purpose outranks any claim ------------------------------
    h.clear()
    h.account("52300001")
    fleet.lease("52300001")
    fleet.down("requested")
    ok = fleet.leased() is None and h.destroyed == ["52300001"]
    print(f"{'ok  ' if ok else 'FAIL'} down() drops the lease it is releasing")
    if not ok:
        fails.append("down() drops the lease")

    print()
    total = 12
    if fails:
        print(f"{total - len(fails)}/{total} lease cases passed")
        return 1
    print(f"{total}/{total} lease cases passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
