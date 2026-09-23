#!/usr/bin/env python3
"""The gallery cap, against a temp directory and never the real one.

Retention is the one piece of this codebase whose job is to delete the user's
renders. The cost of a bug is not a failed job that can be retried - it is
finished work that is gone. So it is exercised here on files this suite makes
itself, and the module's OUT_DIR is repointed for the duration: a suite that
ran against ``output/web`` would be indistinguishable from the bug.

Sizes are megabytes, and the cap is scaled to match. The first version of this
file used literal gigabyte files because the default cap is in gigabytes, and
it filled a 72 GB disk to 100% on the second run. The logic counts bytes and
does not care how big they are; the disk does.

    python scripts/test_retention.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "webapp"))
sys.path.insert(0, str(HERE))

import server  # noqa: E402

MB = 1024 ** 2


@contextmanager
def gallery(cap_mb: float):
    """A throwaway OUT_DIR with the cap scaled to it, always cleaned up.

    The cleanup is the point: every directory here is full of files whose only
    purpose is to take up space, and leaking one per case is how a suite that
    tests a disk limit becomes the thing that exhausts the disk.
    """
    tmp = Path(tempfile.mkdtemp(prefix="retention-"))
    old_out, old_cap = server.OUT_DIR, server.RETENTION_GB
    server.OUT_DIR, server.RETENTION_GB = tmp, cap_mb / 1024
    try:
        yield tmp
    finally:
        server.OUT_DIR, server.RETENTION_GB = old_out, old_cap
        shutil.rmtree(tmp, ignore_errors=True)


def make(root: Path, name: str, mb: float, age_s: float) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "00.png").write_bytes(b"\0" * int(mb * MB))
    t = time.time() - age_s
    os.utime(d / "00.png", (t, t))
    os.utime(d, (t, t))
    return d


def run(cap_mb, jobs, keep=None):
    """Build a gallery, prune it, and report which job dirs survived."""
    with gallery(cap_mb) as tmp:
        for name, mb, age in jobs:
            make(tmp, name, mb, age)
        (tmp / "index.jsonl").write_text('{"id":"a"}\n', encoding="utf-8")
        server._prune_output(keep=keep)
        return (sorted(d.name for d in tmp.iterdir() if d.is_dir()),
                (tmp / "index.jsonl").is_file())


def main() -> int:
    fails: list[str] = []

    def check(name, got, want):
        ok = got == want
        print(f"{'ok  ' if ok else 'FAIL'} {name}")
        if not ok:
            print(f"       got={got}\n      want={want}")
            fails.append(name)

    # Under the cap nothing is touched - the common case, and the one where a
    # bug is silent because there is nothing to see afterwards.
    got, idx = run(50, [("a", 4, 300), ("b", 4, 200)])
    check("under the cap -> nothing deleted", got, ["a", "b"])
    check("index.jsonl is not a job dir", idx, True)

    # Oldest first, and only as far as the cap requires: b and c must survive.
    got, _ = run(9, [("a", 4, 300), ("b", 4, 200), ("c", 4, 100)])
    check("over the cap -> oldest dropped, rest kept", got, ["b", "c"])

    # The job being written is never the victim, even when it is the only
    # thing that could be deleted to get under an impossible cap.
    got, _ = run(0.001, [("a", 2, 300), ("b", 2, 100)], keep="b")
    check("keep survives an impossible cap", got, ["b"])

    # 0 disables: a cap of zero means "no cap", not "delete everything".
    got, _ = run(0, [("a", 4, 300), ("b", 4, 100)])
    check("cap 0 disables retention", got, ["a", "b"])

    # A deleted render must leave the gallery, not a broken thumbnail. This is
    # what lets retention skip rewriting the history file at all.
    with gallery(50) as tmp:
        old_hist = server.HISTORY
        server.HISTORY = tmp / "index.jsonl"
        try:
            make(tmp, "gone", 1, 100)
            server.HISTORY.write_text(
                '{"id":"gone","images":["/results/gone/00.png"],"state":"done"}\n'
                '{"id":"here","images":["/results/here/00.png"],"state":"done"}\n',
                encoding="utf-8")
            make(tmp, "here", 1, 50)
            shutil.rmtree(tmp / "gone")
            check("history drops entries whose files were pruned",
                  [r["id"] for r in server._history()], ["here"])
        finally:
            server.HISTORY = old_hist

    # Nothing may outlive the run. This suite writes files whose only property
    # is size, so a leak here is measured in gigabytes, not in tidiness.
    leaked = list(Path(tempfile.gettempdir()).glob("retention-*"))
    check("no temp directories left behind", leaked, [])

    total = 7
    if fails:
        print(f"\n{total - len(fails)}/{total} retention cases passed")
        return 1
    print(f"\n{total}/{total} retention cases passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
