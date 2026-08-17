#!/usr/bin/env python3
"""
Scene -> collage -> img2img pipeline, all in a single workflow.

Does not upload images to the worker: the scene and the character are
generated, cut out with BiRefNet and composed inside the ComfyUI graph
itself.

Returns 3 images per run:
  a_scene      the clean scene (reusable as a fixed set)
  b_collage    the raw paste, for comparison
  c_fused      after the img2img pass

Usage:
    python scripts/escena.py --denoise 0.4
    python scripts/escena.py --sweep 0.25,0.35,0.45,0.55
"""
from __future__ import annotations

import argparse, asyncio, json, os, sys, urllib.request, uuid
from pathlib import Path

from vastai import Serverless

from config import ENDPOINT_NAME as ENDPOINT, ROOT, registrar_run
from construir_wf import (scale_to, place_box, CANVAS, MARGIN, DETAIL_RES2,
                          add_flags, from_flags)


def api_key() -> str:
    k = os.environ.get("VAST_API_KEY", "").strip()
    if k:
        return k
    for c in (Path.home()/".config"/"vastai"/"vast_api_key", Path.home()/".vast_api_key"):
        if c.is_file() and c.read_text().strip():
            return c.read_text().strip()
    sys.exit("VAST_API_KEY missing")


def build(a) -> dict:
    wf = from_flags(a)
    if a.scene:
        wf["10"]["inputs"]["text"] = a.scene
    if a.character:
        wf["20"]["inputs"]["text"] = a.character
    if a.fusion:
        wf["40"]["inputs"]["text"] = a.fusion
    # the negatives were only editable by hand-patching the json: without them
    # there is no way to push AGAINST something the positive keeps dragging in
    if a.character_neg:
        wf["21"]["inputs"]["text"] = a.character_neg
    if a.fusion_neg:
        wf["41"]["inputs"]["text"] = a.fusion_neg
    wf["13"]["inputs"]["seed"] = a.seed_scene
    wf["23"]["inputs"]["seed"] = a.seed_character
    wf["43"]["inputs"]["seed"] = a.seed_fusion
    wf["43"]["inputs"]["denoise"] = a.denoise
    # single source for the geometry: the same one construir_wf uses when
    # building the graph, so --vertical/--canvas do not get stomped here
    g = place_box(wf, size=a.size, x=a.x, y=a.y,
                  canvas=getattr(a, "canvas", CANVAS),
                  vertical=getattr(a, "vertical", False),
                  horizontal=getattr(a, "horizontal", False))
    if "80" in wf:                      # hd/hd2/hd3 variants: detail crop
        cx = max(0, g["x"] - MARGIN)
        cy = max(0, g["y"] - MARGIN)
        cw = min(g["canvas"] - cx, g["cw"] + 2 * MARGIN)
        ch = min(g["canvas"] - cy, g["ch"] + 2 * MARGIN)
        for n in ("80", "81"):
            wf[n]["inputs"].update(x=cx, y=cy, width=cw, height=ch)
        wf["91"]["inputs"].update(x=cx, y=cy)
        wf["90"]["inputs"].update(width=cw, height=ch)
        if "93" in wf:                  # hd2/hd3: aspect and own prompt
            dw, dh = scale_to(cw, ch, DETAIL_RES2)
            for n in ("82", "84"):
                wf[n]["inputs"].update(width=dw, height=dh)
            if a.detail_prompt:
                wf["93"]["inputs"]["text"] = a.detail_prompt
        else:                           # hd: square scaling, as it was
            for n in ("82", "84"):
                wf[n]["inputs"].update(width=1024, height=1024)
    return wf


async def launch(wf: dict, timeout: float) -> dict:
    cli = Serverless(api_key=api_key())
    try:
        ep = await cli.get_endpoint(name=ENDPOINT)
        return await ep.request(
            "/generate/sync",
            {"input": {"request_id": str(uuid.uuid4()), "workflow_json": wf}},
            cost=100, timeout=timeout)
    finally:
        await cli.close()


def outputs(res) -> list[dict]:
    def find(o):
        if isinstance(o, dict):
            if isinstance(o.get("output"), list) and o["output"]:
                return o["output"]
            for v in o.values():
                f = find(v)
                if f: return f
        if isinstance(o, list):
            for v in o:
                f = find(v)
                if f: return f
        return None
    return find(res) or []


async def one(a, denoise: float, dst: Path) -> None:
    a.denoise = denoise
    wf = build(a)
    print(f"[denoise {denoise}] sending...", flush=True)
    res = await launch(wf, a.timeout)
    outs = outputs(res)
    if not outs:
        print(f"[denoise {denoise}] NO OUTPUTS: {json.dumps(res)[:300]}")
        return
    dst.mkdir(parents=True, exist_ok=True)
    for o in outs:
        name = o.get("filename", "?")
        label = name.split("_")[0]          # a / b / c
        path = dst / f"{label}_d{denoise:.2f}.png"
        urllib.request.urlretrieve(o["url"], path)
        print(f"[denoise {denoise}] {path.name}")
    print(f"[denoise {denoise}] OK ({len(outs)} images)", flush=True)


async def main() -> int:
    p = argparse.ArgumentParser()
    add_flags(p)
    p.add_argument("--scene")
    p.add_argument("--character")
    p.add_argument("--fusion")
    p.add_argument("--character-neg", help="character negative (node 21)")
    p.add_argument("--fusion-neg", help="fusion negative (node 41)")
    p.add_argument("--detail-prompt", help="2nd pass prompt (node 93)")
    p.add_argument("--seed-scene", type=int, default=111111)
    p.add_argument("--seed-character", type=int, default=222222)
    p.add_argument("--seed-fusion", type=int, default=333333)
    p.add_argument("--denoise", type=float, default=0.4)
    p.add_argument("--sweep", help="comma-separated denoise list")
    p.add_argument("--size", type=int, default=640, help="character size in px")
    p.add_argument("--x", type=int, default=200)
    p.add_argument("--y", type=int, default=380)
    p.add_argument("--out", default="output/scenes")
    p.add_argument("--timeout", type=float, default=900.0)
    a = p.parse_args()

    dst = registrar_run(ROOT / a.out, a, {"seeds": (a.seed_scene, a.seed_character,
                                                    a.seed_fusion)})
    values = [float(v) for v in a.sweep.split(",")] if a.sweep else [a.denoise]
    for i, d in enumerate(values):
        await one(a, d, dst)
        if i + 1 < len(values):
            await asyncio.sleep(2)      # margin for the ~1 req/s limit
    print("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
