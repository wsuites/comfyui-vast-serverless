#!/usr/bin/env python3
"""Terminal console for the "mizuki" serverless endpoint.

The same thing ``webapp/`` does, without a browser: submit a render, watch the
worker come up phase by phase, and pull the images down to disk.

    python scripts/mizuki.py gen "1girl, standing" --no-upscale
    python scripts/mizuki.py status
    python scripts/mizuki.py watch

Why this exists next to ``call_endpoint.py``: that one fires a request and
dumps the raw JSON, which is right for scripting but leaves you staring at a
blank terminal for the ~7 minutes a cold start takes, with no way to tell a
slow boot from a hung worker. This adds the progress and saves the images.

The phase rules come from ``vast_state``, shared with the web page, so the two
cannot drift apart.

On progress: the pyworker only exposes ``/generate/sync`` and ``/health``, so
a percent-complete bar is impossible without changing the template. What you
get is *phase* progress, which is where the time actually goes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from argparse import Namespace
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from call_endpoint import RMBG_MODELS, build_workflow, resolve_api_key
from config import ENDPOINT_NAME, ROOT
from vast_state import (LABELS, PHASES, describe, endpoint, extract_image_urls,
                        goes_backwards, instances, unstick, workers)

POLL_SECONDS = 5
OUT_DIR = ROOT / "output" / "cli"

# Kept ASCII on purpose: this runs on a Windows console whose default code
# page mangles anything fancier, and a UnicodeEncodeError in the progress
# line would kill a render that is already paid for.
SPINNER = "|/-\\"


def fmt(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60:02d}:{s % 60:02d}"


# ------------------------------------------------------------------ progress


class Progress:
    """One self-updating status line, plus a permanent line per phase change.

    Falls back to plain appended lines when stdout is not a terminal, so
    ``mizuki.py gen ... > log.txt`` stays readable instead of filling up with
    carriage returns.
    """

    def __init__(self, quiet: bool = False) -> None:
        self.tty = sys.stdout.isatty() and not quiet
        self.quiet = quiet
        self.started = time.time()
        self.phase = "submitting"
        self.detail = "Handing the request to the endpoint"
        self.tick = 0
        self._width = 0

    @property
    def elapsed(self) -> float:
        return time.time() - self.started

    def _clear(self) -> None:
        if self.tty and self._width:
            sys.stdout.write("\r" + " " * self._width + "\r")
            self._width = 0

    def update(self, phase: str, detail: str) -> None:
        """Feed a new state in. Backwards moves and repeats are dropped."""
        if goes_backwards(self.phase, phase):
            return
        if phase == self.phase and detail == self.detail:
            return
        changed = phase != self.phase
        self.phase, self.detail = phase, detail
        if changed:
            self.note(f"[{fmt(self.elapsed)}] {LABELS.get(phase, phase)}: {detail}")
        self.draw()

    def note(self, text: str) -> None:
        """Print a line that stays on screen above the status line."""
        if self.quiet:
            return
        self._clear()
        print(text, flush=True)

    def draw(self) -> None:
        if self.quiet:
            return
        if not self.tty:
            return                      # non-tty already got its note() line
        self.tick += 1
        steps = " > ".join(
            f"[{LABELS[p]}]" if p == self.phase else LABELS[p]
            for p in PHASES if PHASES.index(p) <= max(PHASES.index(self.phase), 0)
        )
        line = (f"{SPINNER[self.tick % len(SPINNER)]} {fmt(self.elapsed)}  "
                f"{steps}  {self.detail}")
        line = line[:150]
        self._clear()
        sys.stdout.write(line)
        sys.stdout.flush()
        self._width = len(line)

    def done(self) -> None:
        self._clear()


async def _state() -> dict:
    """Both Vast views, merged, off the event loop."""
    insts, works = await asyncio.gather(
        asyncio.to_thread(instances),
        asyncio.to_thread(workers),
    )
    return describe(insts, works)


async def _watch(prog: Progress, stop_after: set[str]) -> None:
    """Poll Vast every POLL_SECONDS but redraw every second.

    Two different clocks on purpose: the API is rate limited to roughly one
    request a second and chained calls come back 429, but the elapsed counter
    has to keep moving or a 7 minute cold start looks like a freeze.
    """
    ticks = 0
    while True:
        if ticks % POLL_SECONDS == 0:
            try:
                info = await _state()
            except Exception:
                info = None             # a status display that crashes is worse
            if info and prog.phase not in stop_after:
                prog.update(info["phase"], info["detail"])
        prog.draw()
        ticks += 1
        await asyncio.sleep(1)


# ----------------------------------------------------------------- download


async def _save(urls: list[str], dest: Path, prog: Progress) -> list[Path]:
    import aiohttp

    dest.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    async with aiohttp.ClientSession() as session:
        for i, url in enumerate(urls):
            try:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=120)
                ) as resp:
                    resp.raise_for_status()
                    blob = await resp.read()
            except Exception as exc:
                prog.note(f"  could not download image {i + 1}: {exc}")
                continue
            path = dest / f"{i:02d}.png"
            path.write_bytes(blob)
            saved.append(path)
    return saved


# --------------------------------------------------------------- subcommands


async def _preflight(prog: Progress, enabled: bool = True) -> None:
    """Clear a worker stranded on a full machine before spending the wait.

    Run before submitting rather than after timing out: a stopped instance
    whose host filled up never rents a replacement on its own, so the request
    would sit in the queue for the full ``--timeout`` and come back with
    nothing. The probe is two CLI calls and it no-ops on a healthy endpoint,
    so it costs a second to skip a wasted 15 minutes.
    """
    if not enabled:
        return
    report = await asyncio.to_thread(unstick)
    if not report.get("acted"):
        return
    if report.get("destroyed"):
        prog.note(f"worker was stranded ({report['reason']}) - destroyed "
                  f"instance {report['instance']}; the autoscaler will rent "
                  f"another machine. Expect a cold start.")
    else:
        prog.note(f"worker looks stranded ({report['reason']}) but the "
                  f"replacement failed: {report['detail']}")


async def cmd_gen(args: argparse.Namespace) -> int:
    from vastai import Serverless

    job_id = uuid.uuid4().hex[:12]
    dest = OUT_DIR / job_id
    prog = Progress(quiet=args.json)

    wf_args = Namespace(
        workflow=args.workflow, prompt=args.prompt, negative=args.negative,
        seed=args.seed, width=args.width, height=args.height, batch=args.batch,
        steps=args.steps, cfg=args.cfg, no_upscale=args.no_upscale,
        remove_bg=args.remove_bg, bg_refine=args.bg_refine,
        bg_sensitivity=args.bg_sensitivity, bg_blur=args.bg_blur,
        bg_offset=args.bg_offset,
    )

    prog.note(f"job {job_id} -> {dest}")
    await _preflight(prog, enabled=not args.no_preflight)
    workflow = await asyncio.to_thread(build_workflow, wf_args)
    payload = {"input": {"request_id": str(uuid.uuid4()), "workflow_json": workflow}}

    watcher = asyncio.create_task(_watch(prog, stop_after={"saving", "done"}))
    client = Serverless(api_key=resolve_api_key())
    raw: Any = None
    latency = None
    try:
        ep = await client.get_endpoint(name=ENDPOINT_NAME)
        started = time.time()
        raw = await ep.request("/generate/sync", payload,
                               cost=args.cost, timeout=args.timeout)
        latency = time.time() - started

        urls = extract_image_urls(raw)
        if not urls:
            prog.update("error", "worker replied without any image URL")
            raise RuntimeError(
                f"no image URL in the reply; raw response in {dest / 'raw.json'}")

        prog.update("saving", f"downloading {len(urls)} image(s) from R2")
        saved = await _save(urls, dest, prog)
        prog.update("done", f"finished in {latency:.1f}s")
        watcher.cancel()
        prog.done()

        if args.json:
            print(json.dumps({
                "job": job_id, "latency": latency,
                "images": [str(p) for p in saved], "urls": urls,
            }, indent=2))
        else:
            print(f"\n{len(saved)} image(s) in {dest}  (worker {latency:.1f}s)")
            for p in saved:
                print(f"  {p}")
            if len(saved) < len(urls):
                print("  note: the R2 links expire in 7 days")
        return 0
    except Exception as exc:
        watcher.cancel()
        prog.done()
        print(f"\nFAILED after {fmt(prog.elapsed)}: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1
    finally:
        watcher.cancel()
        try:
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "raw.json").write_text(
                json.dumps(raw, indent=2, default=str), encoding="utf-8")
        except Exception:
            pass
        await client.close()


async def cmd_status(args: argparse.Namespace) -> int:
    info, ep = await asyncio.gather(_state(), asyncio.to_thread(endpoint))
    if args.json:
        print(json.dumps({"endpoint": ep, **info}, indent=2, default=str))
        return 0

    w = info["worker"]
    print(f"endpoint {ENDPOINT_NAME}  ({ep.get('id', '?')})")
    if not w:
        print("  no machine rented - nothing is being charged")
        return 0
    print(f"  phase    {LABELS.get(info['phase'], info['phase'])}: {info['detail']}")
    print(f"  gpu      {w.get('gpu')}  machine {w.get('machine')}  "
          f"instance {w.get('id')}")
    print(f"  price    ${float(w.get('dph') or 0):.3f}/h")
    if "hours" in w:
        print(f"  up       {fmt(w['hours'] * 3600)}   spent ${w['spent']:.2f}")
    # 'status' is reported but not trusted: a worker answering /health in 0.4s
    # still says "offline" whenever its heartbeat to the autoscaler lapses.
    print(f"  ready    {w.get('ready')}   in flight {w.get('reqs')}   "
          f"(autoscaler says '{w.get('status')}')")
    return 0


async def cmd_watch(args: argparse.Namespace) -> int:
    prog = Progress()
    prog.note("watching the endpoint - ctrl-c to stop")
    try:
        await _watch(prog, stop_after=set())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        prog.done()
    return 0


# --------------------------------------------------------------------- main


async def cmd_unstick(args: argparse.Namespace) -> int:
    report = await asyncio.to_thread(unstick, args.dry_run)
    if not report.get("acted"):
        print(f"nothing to do: {report['detail']}")
        return 0
    print(f"stranded: {report['reason']}")
    print(f"instance {report['instance']} on machine {report['machine']}")
    print(report["detail"])
    if args.dry_run:
        return 0
    return 0 if report.get("destroyed") else 1


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mizuki", description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gen", help="render an image and download it")
    g.add_argument("prompt", nargs="?", help="positive prompt")
    g.add_argument("--negative", help="blank = the workflow's own default")
    g.add_argument("--workflow", default=str(ROOT / "workflows" / "wf.json"))
    g.add_argument("--seed", type=int, help="omit for a random one")
    g.add_argument("--width", type=int, default=1024)
    g.add_argument("--height", type=int, default=1024)
    g.add_argument("--batch", type=int, default=1)
    g.add_argument("--steps", type=int)
    g.add_argument("--cfg", type=float)
    g.add_argument("--no-upscale", action="store_true",
                   help="skip the upscale pass (~18s instead of ~60s)")
    g.add_argument("--remove-bg", choices=sorted(RMBG_MODELS))
    g.add_argument("--bg-refine", action="store_true")
    g.add_argument("--bg-sensitivity", type=float, default=1.0)
    g.add_argument("--bg-blur", type=int, default=0)
    g.add_argument("--bg-offset", type=int, default=0)
    g.add_argument("--cost", type=int, default=100,
                   help="cost units for the autoscaler")
    g.add_argument("--timeout", type=float, default=900.0,
                   help="seconds; a cold start alone takes ~7 min")
    g.add_argument("--json", action="store_true", help="machine-readable output")
    g.add_argument("--no-preflight", action="store_true",
                   help="do not replace a worker stranded on a full machine")
    g.set_defaults(fn=cmd_gen)

    u = sub.add_parser("unstick",
                       help="replace a worker stranded on a full machine")
    u.add_argument("--dry-run", action="store_true",
                   help="report what would happen without destroying anything")
    u.set_defaults(fn=cmd_unstick)

    s = sub.add_parser("status", help="worker, phase and accumulated cost")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    w = sub.add_parser("watch", help="follow the worker until ctrl-c")
    w.set_defaults(fn=cmd_watch)
    return p


def main() -> int:
    args = parser().parse_args()
    if args.cmd == "gen" and not args.prompt:
        # build_workflow leaves the workflow's own prompt in place when this is
        # None, which silently renders something other than what was asked for.
        print("a prompt is required: mizuki.py gen \"1girl, standing\"",
              file=sys.stderr)
        return 2
    try:
        return asyncio.run(args.fn(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
