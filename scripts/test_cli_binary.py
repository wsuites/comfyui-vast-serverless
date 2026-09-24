#!/usr/bin/env python3
"""Pin how the Vast CLI is found, and what happens when it is not there.

This suite exists because of a bug that cost a morning and could have cost a
GPU. `vastai` is a console script pip installs beside the interpreter, and
every call used to spell it as a bare name on PATH. Under systemd the PATH is
`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/snap/bin` and the venv is
not in it, so `subprocess` raised FileNotFoundError, `_vastai` caught it, and
`instances()` returned `[]`.

That empty list is indistinguishable from an empty account. The reaper ran for
seven hours, logged nothing, found nothing to reap and would have found nothing
if a worker had been billing the whole time; the page and `fleet.py status`
agreed with it, because they read the same swallowed error.

So two things are asserted here, and neither needs a Vast account:

* where the binary is looked for, in order, including the systemd case;
* that the callers which spend money report a missing CLI instead of reading
  it as "nothing rented" - and that the daemon refuses to start at all.

    python scripts/test_cli_binary.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import vast_state as vs  # noqa: E402
import fleet  # noqa: E402

WIN = sys.platform == "win32"
EXE = "vastai.exe" if WIN else "vastai"

# The PATH systemd hands a unit that has no Environment=PATH of its own.
SYSTEMD_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/snap/bin"


class Layout:
    """A throwaway venv: an interpreter, and maybe a CLI beside it."""

    def __init__(self, tmp: Path, cli_beside: bool):
        self.bindir = tmp / (".venv/Scripts" if WIN else ".venv/bin")
        self.bindir.mkdir(parents=True)
        (self.bindir / ("python.exe" if WIN else "python")).write_text("#\n")
        self.cli = self.bindir / EXE
        if cli_beside:
            self.cli.write_text("#!/bin/sh\n")
            # `shutil.which` skips a file it cannot execute, so the PATH case
            # only means anything with the bit set. Windows ignores it.
            self.cli.chmod(0o755)

    @property
    def interpreter(self) -> str:
        return str(self.bindir / ("python.exe" if WIN else "python"))


def resolve(interpreter: str, path: str, override: str | None = None) -> str:
    """vastai_bin() as it would answer for that interpreter and PATH."""
    saved_exe, saved_env = sys.executable, dict(os.environ)
    saved_cfg = vs.ENV.get("VASTAI_BIN")
    try:
        sys.executable = interpreter
        os.environ["PATH"] = path
        os.environ.pop("VASTAI_BIN", None)
        vs.ENV.pop("VASTAI_BIN", None)
        if override is not None:
            vs.ENV["VASTAI_BIN"] = override
        vs.vastai_bin.cache_clear()
        return vs.vastai_bin()
    finally:
        sys.executable = saved_exe
        os.environ.clear()
        os.environ.update(saved_env)
        vs.ENV.pop("VASTAI_BIN", None)
        if saved_cfg is not None:
            vs.ENV["VASTAI_BIN"] = saved_cfg
        vs.vastai_bin.cache_clear()


def check(name: str, got, want) -> bool:
    # Windows paths compare the way the filesystem does: `shutil.which` hands
    # back the extension as it is registered (`vastai.EXE`), not as it was
    # written on disk.
    if WIN and isinstance(got, str) and isinstance(want, str):
        ok = got.casefold() == want.casefold()
    else:
        ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"       got={got!r}\n       want={want!r}")
    return ok


# --- the second door: a CLI that is found, runs, and is not logged in -------

AUTH_CASES = 15
NL = chr(10)
Q = chr(34)


def fake_cli(tmp: Path, body: str) -> str:
    """A `vastai` that is present, runs, and answers `body` on stdout."""
    d = tmp / "fakebin"
    d.mkdir(parents=True, exist_ok=True)
    py = d / "vastai_fake.py"
    py.write_text("import sys" + NL + "sys.stdout.write(%r)" % body + NL)
    exe = d / ("vastai.cmd" if WIN else "vastai")
    if WIN:
        exe.write_text("@echo off" + NL
                       + Q + sys.executable + Q + " " + Q + str(py) + Q + " %*" + NL)
    else:
        exe.write_text("#!/bin/sh" + NL
                       + "exec " + sys.executable + " " + str(py) + NL)
        exe.chmod(0o755)
    return str(exe)


def auth_cases() -> list[str]:
    """An unauthenticated CLI must never read back as an empty account.

    This is the bug that `vastai_bin` already documents, arriving the other
    way round. There the binary was missing; here it is present and perfect
    and has no API key, so Vast answers every account-level call with a 403
    wrapped in a 200-shaped body. Both end as `[]`, and `[]` is what a reaper
    reads as "nothing to release".
    """
    fails: list[str] = []
    denied = '{"error": true, "status_code": 403, "msg": "This action requires login."}'
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        saved = vs.ENV.get("VASTAI_BIN")
        for name, body, want_ok in (
            ("a 403 is not an empty fleet", denied, False),
            ("an empty account still reads empty", "[]", True),
            ("a real listing reads through", '[{"id": 1, "label": "x"}]', True),
        ):
            vs.ENV["VASTAI_BIN"] = fake_cli(tmp / name.replace(" ", "_"), body)
            vs.vastai_bin.cache_clear()
            ok, why = vs.authenticated()
            if ok is not want_ok:
                fails.append(f"{name}: authenticated() said {ok!r} ({why})")
            try:
                got = vs.instances_strict()
                blind = False
            except vs.VastBlind:
                blind = True
                got = []
            if blind is want_ok:
                fails.append(f"{name}: instances_strict() blind={blind!r}")
            # The lenient accessor keeps its old shape for the status displays.
            if not isinstance(vs.instances(), list):
                fails.append(f"{name}: instances() stopped returning a list")
            # And the reaper's eye must refuse rather than report an empty deck.
            try:
                fleet.ours()
                reaped_blind = False
            except vs.VastBlind:
                reaped_blind = True
            except Exception:
                reaped_blind = True
            if reaped_blind is want_ok:
                fails.append(f"{name}: fleet.ours() blind={reaped_blind!r}")
        # A daemon that cannot read the account exits instead of idling.
        vs.ENV["VASTAI_BIN"] = fake_cli(tmp / "daemon", denied)
        vs.vastai_bin.cache_clear()
        if fleet.daemon(interval=0.01) != 1:
            fails.append("the daemon runs blind when the CLI is not logged in")
        # And a template lookup says so instead of killing its host process.
        try:
            fleet.template()
            fails.append("template() returned on an unreadable account")
        except SystemExit:
            fails.append("template() still raises SystemExit - it runs in the API")
        except Exception as exc:
            if "authenticated" not in str(exc):
                fails.append(f"template() blamed the wrong thing: {exc}")
        if saved is None:
            vs.ENV.pop("VASTAI_BIN", None)
        else:
            vs.ENV["VASTAI_BIN"] = saved
        vs.vastai_bin.cache_clear()
    return fails


def main() -> int:
    fails: list[str] = []

    def case(name: str, got, want):
        if not check(name, got, want):
            fails.append(name)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        venv = Layout(tmp / "with", cli_beside=True)
        bare = Layout(tmp / "without", cli_beside=False)

        # The whole point: the venv wins even when the PATH knows nothing.
        case("found beside the interpreter, PATH irrelevant",
             resolve(venv.interpreter, SYSTEMD_PATH), str(venv.cli))

        # The regression itself. A login shell finds it; the unit does not.
        case("the systemd PATH alone finds nothing",
             resolve(bare.interpreter, SYSTEMD_PATH), "")
        case("on PATH is still honoured",
             resolve(bare.interpreter, str(venv.bindir)), str(venv.cli))

        # An override is for a CLI outside the venv, and a wrong one must read
        # as missing rather than being handed to subprocess to fail later.
        case("VASTAI_BIN overrides the lookup",
             resolve(bare.interpreter, SYSTEMD_PATH, override=str(venv.cli)),
             str(venv.cli))
        case("VASTAI_BIN pointing nowhere is missing, not a path",
             resolve(bare.interpreter, str(venv.bindir),
                     override=str(tmp / "nope" / EXE)), "")

        # With no CLI, the calls that spend money say so. `instances()` may
        # still answer [] - it feeds a status display - but nothing that
        # destroys, creates or supervises is allowed to read that as truth.
        saved = vs.vastai_bin
        try:
            vs.vastai_bin = lambda: ""  # type: ignore[assignment]
            ok, out = vs._vastai_raw("destroy", "instance", "1", "-y")
            case("a mutating call reports the missing CLI", ok, False)
            case("  and says which piece is missing", "vastai" in out, True)

            ok, out = fleet.cli("destroy", "instance", "1", "-y")
            case("fleet.cli reports it too", ok, False)
            case("  and says which piece is missing", "vastai" in out, True)

            # The one that matters: a reaper that cannot see the account must
            # not sit there looking healthy. Exiting non-zero is what turns it
            # into a restart loop somebody can find in the journal.
            case("the daemon refuses to run blind",
                 fleet.daemon(interval=0.01), 1)
        finally:
            vs.vastai_bin = saved  # type: ignore[assignment]

    fails.extend(auth_cases())

    total = 10 + AUTH_CASES
    if fails:
        print(f"\n{total - len(fails)}/{total} CLI binary cases passed")
        return 1
    print(f"\n{total}/{total} CLI binary cases passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
