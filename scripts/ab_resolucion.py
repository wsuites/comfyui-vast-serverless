#!/usr/bin/env python3
"""A/B of the character resolution levers.

Sweeps the combinations of --vertical / --canvas / --upscale (and --bbox when
implemented) with the SAME seeds, and builds a comparison sheet with the final
composite of each one.

    python scripts/ab_resolucion.py                      # all 8 combinations
    python scripts/ab_resolucion.py --combos base,v,vl   # just some
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path

from vastai import Serverless

from config import ENDPOINT_NAME as ENDPOINT, api_key, ROOT, registrar_run
from construir_wf import CANVAS
import escena

# name -> flags stomped over the defaults
COMBOS: dict[str, dict] = {
    "base": {},
    "v":    {"vertical": True},
    "l":    {"canvas": 1536},
    "u":    {"upscale": True},
    "vl":   {"vertical": True, "canvas": 1536},
    "vu":   {"vertical": True, "upscale": True},
    "lu":   {"canvas": 1536, "upscale": True},
    "vlu":  {"vertical": True, "canvas": 1536, "upscale": True},
    # bbox: the lever expected to pay off the most, and the last to arrive.
    # 'vb' is the recommended combination (vertical + crop to silhouette).
    "b":    {"bbox": True},
    "vb":   {"vertical": True, "bbox": True},
    "vbu":  {"vertical": True, "bbox": True, "upscale": True},
    "vblu": {"vertical": True, "bbox": True, "canvas": 1536, "upscale": True},
}


def args_for(combo: str, a) -> argparse.Namespace:
    """Copies the base args applying the combo toggles."""
    n = argparse.Namespace(**vars(a))
    n.vertical = False
    n.canvas = CANVAS
    n.bbox = False
    n.upscale = False
    for k, v in COMBOS[combo].items():
        setattr(n, k, v)
    return n


async def one(cli, combo: str, a, dst: Path, times: dict[str, float]) -> None:
    n = args_for(combo, a)
    wf = escena.build(n)
    print(f"[{combo}] sending ({len(wf)} nodes)...", flush=True)
    t0 = time.monotonic()
    ep = await cli.get_endpoint(name=ENDPOINT)
    res = await ep.request(
        "/generate/sync",
        {"input": {"request_id": str(uuid.uuid4()), "workflow_json": wf}},
        cost=100,
        timeout=a.timeout,
    )
    times[combo] = time.monotonic() - t0
    outs = escena.outputs(res)
    if not outs:
        print(f"[{combo}] NO OUTPUTS: {json.dumps(res)[:400]}", flush=True)
        return
    for o in outs:
        pre = o.get("filename", "?").split("_")[0]      # a / b / c / h
        urllib.request.urlretrieve(o["url"], dst / f"{pre}_{combo}.png")
    print(f"[{combo}] OK {times[combo]:.1f}s ({len(outs)} images)", flush=True)


def sheet(dst: Path, combos: list[str], pre: str, out: Path) -> None:
    from PIL import Image, ImageDraw

    ims = [(c, Image.open(dst / f"{pre}_{c}.png").convert("RGB"))
           for c in combos if (dst / f"{pre}_{c}.png").is_file()]
    if not ims:
        return
    h = max(im.height for _, im in ims)
    ws = [round(im.width * h / im.height) for _, im in ims]
    out = Image.new("RGB", (sum(ws), h + 22), (255, 255, 255))
    d = ImageDraw.Draw(out)
    x = 0
    for (c, im), w in zip(ims, ws):
        out.paste(im.resize((w, h), Image.LANCZOS), (x, 22))
        d.text((x + 6, 6), c, fill=(0, 0, 0))
        x += w
    out.save(out)
    print(f"SHEET {out.name} ({len(ims)} variants)")


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--combos", default=",".join(COMBOS))
    p.add_argument("--scene")
    p.add_argument("--character")
    p.add_argument("--fusion")
    p.add_argument("--detail-prompt")
    p.add_argument("--pose", default="dwpose")
    p.add_argument("--detail", default="hd2")
    p.add_argument("--face", action="store_true", default=True)
    p.add_argument("--hands", default=None)
    p.add_argument("--no-mask", action="store_true")
    p.add_argument("--seed-scene", type=int, default=111111)
    p.add_argument("--seed-character", type=int, default=222222)
    p.add_argument("--seed-fusion", type=int, default=333333)
    p.add_argument("--denoise", type=float, default=0.4)
    p.add_argument("--size", type=int, default=640)
    p.add_argument("--x", type=int, default=200)
    p.add_argument("--y", type=int, default=380)
    p.add_argument("--out", default="output/ab-resolution")
    p.add_argument("--timeout", type=float, default=1200.0)
    a = p.parse_args()

    combos = [c.strip() for c in a.combos.split(",") if c.strip()]
    for c in combos:
        if c not in COMBOS:
            sys.exit(f"unknown combo: {c} (available: {', '.join(COMBOS)})")

    dst = registrar_run(ROOT / a.out, a, {"combos": combos, "denoise": a.denoise})
    times: dict[str, float] = {}
    cli = Serverless(api_key=api_key())
    try:
        for i, c in enumerate(combos):
            await one(cli, c, a, dst, times)
            if i + 1 < len(combos):
                await asyncio.sleep(2)          # ~1 req/s limit
    finally:
        await cli.close()

    print("\nTIMES")
    for c, t in times.items():
        print(f"  {c:5s} {t:6.1f}s")
    try:
        for pre, name in (("c", "fused"), ("h", "upscale")):
            sheet(dst, combos, pre, dst / f"ab_resolution_{name}.png")
    except ImportError:
        print("Pillow missing: no sheets")
    print("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
