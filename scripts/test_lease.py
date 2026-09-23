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
        fleet.destroy = self._destroy
        fleet.over_ceiling = lambda inst: False
        vs.instances = lambda: self.insts
        self.insts: list[dict] = []

    def _destroy(self, iid, why: str = "") -> bool:
        self.destroyed.append(str(iid))
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
        fleet.lease_clear()
        fleet.save_state({})


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="lease-"))
    h = Harness(tmp)
    fails: list[str] = []

    def check(name: str, want_destroyed: list[str], want_released: bool = False,
              result: dict | None = None) -> None:
        got = sorted(h.destroyed)
        ok = got == sorted(want_destroyed)
        released = bool((result or {}).get("released"))
        ok = ok and released == want_released
        print(f"{'ok  ' if ok else 'FAIL'} {name}")
        if not ok:
            fails.append(name)
            print(f"       destroyed={got} want={sorted(want_destroyed)} "
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
    check("idle past IDLE_AFTER, no lease -> released", ["52300001"],
          want_released=True, result=r)

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
    check("render lease expired with the process -> released", ["52300001"],
          want_released=True, result=r)

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
    total = 9
    if fails:
        print(f"{total - len(fails)}/{total} lease cases passed")
        return 1
    print(f"{total}/{total} lease cases passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
