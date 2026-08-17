#!/usr/bin/env python3
"""A/B of the second inpaint pass: hd vs hd2 vs hd3.

Sends the same workflow with the same seeds changing only the detail variant,
times each request and builds two comparison sheets: the full image and a 200%
zoom on the face, which is where you can see whether the pass works.

    python scripts/ab_detalle.py
    python scripts/ab_detalle.py --variants hd,hd2,hd3 --denoise 0.85
"""
from __future__ import annotations

import argparse, asyncio, json, sys, time, urllib.request, uuid
from pathlib import Path

from vastai import Serverless

from config import ENDPOINT_NAME as ENDPOINT, api_key, ROOT, registrar_run
from construir_wf import build as arm, VARIANTS

# regions of interest with the default values (x=200 y=380 size=640)
ZOOMS = {"face": (330, 400, 570, 640),
         "hands": (600, 850, 840, 1000)}


def build(variant: str, a) -> dict:
    wf = arm(pose=a.pose, **VARIANTS[variant])
    if a.character:
        wf["20"]["inputs"]["text"] = a.character
    if a.fusion:
        wf["40"]["inputs"]["text"] = a.fusion
    wf["13"]["inputs"]["seed"] = a.seed_scene
    wf["23"]["inputs"]["seed"] = a.seed_character
    wf["43"]["inputs"]["seed"] = a.seed_fusion
    wf["43"]["inputs"]["denoise"] = a.denoise
    return wf


def outputs(res) -> list[dict]:
    def find(o):
        if isinstance(o, dict):
            if isinstance(o.get("output"), list) and o["output"]:
                return o["output"]
            for v in o.values():
                if (f := find(v)):
                    return f
        if isinstance(o, list):
            for v in o:
                if (f := find(v)):
                    return f
        return None
    return find(res) or []


def sheets(dst: Path, variants: list[str], out: Path) -> None:
    from PIL import Image, ImageDraw
    ims = [(v, Image.open(dst / f"f_{v}.png").convert("RGB"))
           for v in variants if (dst / f"f_{v}.png").is_file()]
    if not ims:
        return
    base = dst / "c_base.png"
    if base.is_file():
        ims.insert(0, ("no pass", Image.open(base).convert("RGB")))

    def sheet(name: str, crop, scale: int) -> None:
        pieces = [(v, im.crop(crop) if crop else im) for v, im in ims]
        w, h = pieces[0][1].size
        w, h = w * scale, h * scale
        out = Image.new("RGB", (w * len(pieces), h + 22), (255, 255, 255))
        d = ImageDraw.Draw(out)
        for i, (v, im) in enumerate(pieces):
            out.paste(im.resize((w, h), Image.NEAREST if scale > 1 else Image.LANCZOS),
                      (i * w, 22))
            d.text((i * w + 6, 6), v, fill=(0, 0, 0))
        out.save(out / name)
        print(f"SHEET {name}")

    sheet("ab_detalle_full.png", None, 1)
    for name, box in ZOOMS.items():
        sheet(f"ab_detalle_{name}.png", box, 3)


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--pose", default="dwpose")
    p.add_argument("--character")
    p.add_argument("--fusion")
    p.add_argument("--zoom", help="extra box x1,y1,x2,y2 for the sheets")
    p.add_argument("--variants", default="hd,hd2,hd3")
    p.add_argument("--denoise", type=float, default=0.85)
    p.add_argument("--seed-scene", type=int, default=111111)
    p.add_argument("--seed-character", type=int, default=222222)
    p.add_argument("--seed-fusion", type=int, default=333333)
    p.add_argument("--out", default="output/ab-detail")
    p.add_argument("--timeout", type=float, default=900.0)
    a = p.parse_args()

    variants = [v.strip() for v in a.variants.split(",") if v.strip()]
    dst = registrar_run(ROOT / a.out, a, {"variants": variants, "denoise": a.denoise})

    times: dict[str, float] = {}
    cli = Serverless(api_key=api_key())
    try:
        ep = await cli.get_endpoint(name=ENDPOINT)
        for i, v in enumerate(variants):
            print(f"[{v}] sending...", flush=True)
            t0 = time.monotonic()
            res = await ep.request(
                "/generate/sync",
                {"input": {"request_id": str(uuid.uuid4()),
                           "workflow_json": build(v, a)}},
                cost=100, timeout=a.timeout)
            times[v] = time.monotonic() - t0
            outs = outputs(res)
            if not outs:
                print(f"[{v}] NO OUTPUTS: {json.dumps(res)[:400]}", flush=True)
                continue
            for o in outs:
                pre = o.get("filename", "?").split("_")[0]
                if pre == "f":
                    ruta = dst / f"f_{v}.png"
                elif pre == "g":                    # MeshGraphormer depth
                    ruta = dst / f"g_depth_{v}.png"
                elif pre == "c":
                    ruta = dst / "c_base.png"       # identical in all variants
                else:
                    continue
                urllib.request.urlretrieve(o["url"], ruta)
            print(f"[{v}] OK {times[v]:.1f}s ({len(outs)} images)", flush=True)
            if i + 1 < len(variants):
                await asyncio.sleep(2)              # ~1 req/s limit
    finally:
        await cli.close()

    print("\nTIMES (includes cold start on the first one)")
    for v, t in times.items():
        print(f"  {v:5s} {t:6.1f}s")
    if a.zoom:
        ZOOMS["zoom"] = tuple(int(v) for v in a.zoom.split(","))
    try:
        sheets(dst, variants, dst)
    except ImportError:
        print("Pillow missing: no sheets")
    print("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
