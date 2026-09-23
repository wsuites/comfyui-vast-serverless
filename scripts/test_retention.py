#!/usr/bin/env python3
"""The gallery cap, against a temp directory and never the real one.

Retention is the one piece of this codebase whose job is to delete the user's
renders. The cost of a bug is not a failed job that can be retried - it is
finished work that is gone. So it is exercised here on files this suite makes
itself, and the module's OUT_DIR is repointed for the duration: a suite that
ran against ``output/web`` would be indistinguishable from the bug.

    python scripts/test_retention.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "webapp"))
sys.path.insert(0, str(HERE))

import server  # noqa: E402

MB = 1024 ** 2


def make(root: Path, name: str, mb: int, age_s: float) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "00.png").write_bytes(b"\0" * (mb * MB))
    t = time.time() - age_s
    import os
    os.utime(d / "00.png", (t, t))
    os.utime(d, (t, t))
    return d


def run(cap_gb: float, jobs, keep=None):
    """Build a gallery, prune it, and report which job dirs survived."""
    tmp = Path(tempfile.mkdtemp(prefix="retention-"))
    old_out, old_cap = server.OUT_DIR, server.RETENTION_GB
    server.OUT_DIR, server.RETENTION_GB = tmp, cap_gb
    try:
        for name, mb, age in jobs:
            make(tmp, name, mb, age)
        (tmp / "index.jsonl").write_text('{"id":"a"}\n', encoding="utf-8")
        server._prune_output(keep=keep)
        return (sorted(d.name for d in tmp.iterdir() if d.is_dir()),
                (tmp / "index.jsonl").is_file())
    finally:
        server.OUT_DIR, server.RETENTION_GB = old_out, old_cap


def main() -> int:
    fails: list[str] = []

    def check(name, got, want):
        ok = got == want
        print(f"{'ok  ' if ok else 'FAIL'} {name}")
        if not ok:
            print(f"       got={got}\n      want={want}")
            fails.append(name)

    one_gb = 1024

    # Under the cap nothing is touched - the common case, and the one where a
    # bug is silent because there is nothing to see afterwards.
    got, idx = run(5, [("a", 100, 300), ("b", 100, 200)])
    check("under the cap -> nothing deleted", got, ["a", "b"])
    check("index.jsonl is not a job dir", idx, True)

    # Oldest first, and only as far as the cap requires: b and c must survive.
    got, _ = run(2, [("a", one_gb, 300), ("b", one_gb, 200), ("c", one_gb, 100)])
    check("over the cap -> oldest dropped, rest kept", got, ["b", "c"])

    # The job being written is never the victim, even when it is the only
    # thing that could be deleted to get under an impossible cap.
    got, _ = run(0.001, [("a", 100, 300), ("b", 100, 100)], keep="b")
    check("keep survives an impossible cap", got, ["b"])

    # 0 disables: a cap of zero means "no cap", not "delete everything".
    got, _ = run(0, [("a", one_gb, 300), ("b", one_gb, 100)])
    check("cap 0 disables retention", got, ["a", "b"])

    # A deleted render must leave the gallery, not a broken thumbnail. This is
    # what lets retention skip rewriting the history file at all.
    tmp = Path(tempfile.mkdtemp(prefix="retention-hist-"))
    old_out, old_hist = server.OUT_DIR, server.HISTORY
    server.OUT_DIR, server.HISTORY = tmp, tmp / "index.jsonl"
    try:
        make(tmp, "gone", 1, 100)
        server.HISTORY.write_text(
            '{"id":"gone","images":["/results/gone/00.png"],"state":"done"}\n'
            '{"id":"here","images":["/results/here/00.png"],"state":"done"}\n',
            encoding="utf-8")
        make(tmp, "here", 1, 50)
        (tmp / "gone" / "00.png").unlink()
        (tmp / "gone").rmdir()
        check("history drops entries whose files were pruned",
              [r["id"] for r in server._history()], ["here"])
    finally:
        server.OUT_DIR, server.HISTORY = old_out, old_hist

    total = 6
    if fails:
        print(f"\n{total - len(fails)}/{total} retention cases passed")
        return 1
    print(f"\n{total}/{total} retention cases passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
