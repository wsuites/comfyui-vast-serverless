#!/usr/bin/env python3
"""
Frame sequence: a character running left to right across a fixed scene.

Uses the configuration that gave the best results in testing:
  fixed scene + collage + mask + DWPose + high denoise + double inpaint.

    python scripts/correr.py                # 8 frames
    python scripts/correr.py --frames 12
"""
from __future__ import annotations

import argparse, asyncio, json, os, sys, urllib.request, uuid
from pathlib import Path

from vastai import Serverless

from config import ENDPOINT_NAME as ENDPOINT, ROOT, registrar_run
from construir_wf import scale_to, DETAIL_RES2, add_flags, from_flags, CANVAS, MARGIN

IDENTITY = ("(souryuu asuka langley:1.3), (asuka langley:1.2), "
            "neon genesis evangelion, long red hair, ahoge, (blue eyes:1.3), "
            "two side up, hair tubes, hair ribbons, school uniform, "
            "white shirt, red ribbon, pleated skirt")

SCENE = ("masterpiece, best quality, detailed background, empty city street, "
         "long sidewalk, buildings on both sides, afternoon light, "
         "side view, horizontal composition, no people")
NEG_SCENE = ("1girl, person, people, character, worst quality, blurry, lowres, "
             "watermark, text")

# running cycle: repeats every 4 frames
CYCLE = [
    "running to the right, side view, left leg forward, right arm forward",
    "running to the right, side view, mid stride, both feet off the ground",
    "running to the right, side view, right leg forward, left arm forward",
    "running to the right, side view, pushing off with back foot",
]

NEG_CHARACTER = ("worst quality, blurry, lowres, bad hands, bad anatomy, "
                 "watermark, text, cropped, out of frame, cut off, close-up, "
                 "portrait, upper body, facing viewer, front view")
NEG_FUSION = ("worst quality, blurry, lowres, bad hands, bad anatomy, pasted, "
              "cutout, sticker, floating, watermark, text, standing still")


def api_key() -> str:
    k = os.environ.get("VAST_API_KEY", "").strip()
    if k:
        return k
    for c in (Path.home()/".config"/"vastai"/"vast_api_key", Path.home()/".vast_api_key"):
        if c.is_file() and c.read_text().strip():
            return c.read_text().strip()
    sys.exit("VAST_API_KEY missing")


def build(base: dict, a, x: int, pose: str) -> dict:
    wf = json.loads(json.dumps(base))
    y, size = a.y, a.size

    wf["10"]["inputs"]["text"] = SCENE
    wf["11"]["inputs"]["text"] = NEG_SCENE
    wf["20"]["inputs"]["text"] = (f"masterpiece, best quality, solo, 1girl, {IDENTITY}, "
                                  f"{pose}, (full body:1.3), full body shot, "
                                  "entire body visible, feet visible, simple background")
    wf["21"]["inputs"]["text"] = NEG_CHARACTER
    wf["40"]["inputs"]["text"] = (f"masterpiece, best quality, solo, 1girl, {IDENTITY}, "
                                  f"{pose}, city street, sidewalk, afternoon light, "
                                  "coherent lighting, contact shadow, motion, dynamic")
    wf["41"]["inputs"]["text"] = NEG_FUSION

    wf["13"]["inputs"]["seed"] = a.seed_scene          # identical scene always
    wf["23"]["inputs"]["seed"] = a.seed_character      # same character base
    wf["43"]["inputs"]["seed"] = a.seed_fusion
    wf["43"]["inputs"]["denoise"] = a.denoise

    for n in ("26", "28"):
        wf[n]["inputs"]["width"] = size
        wf[n]["inputs"]["height"] = size
    for n in ("30", "51", "77"):                        # collage, mask, skeleton
        if n in wf:
            wf[n]["inputs"]["x"] = x
            wf[n]["inputs"]["y"] = y
    if "75" in wf:
        wf["75"]["inputs"]["width"] = size
        wf["75"]["inputs"]["height"] = size

    # the double-inpaint crop follows the character
    if "80" in wf:
        cx = max(0, x - MARGIN)
        cy = max(0, y - MARGIN)
        cw = min(CANVAS - cx, size + 2 * MARGIN)
        ch = min(CANVAS - cy, size + 2 * MARGIN)
        for n, campos in (("80", ("x", "y", "width", "height")),
                          ("81", ("x", "y", "width", "height")),
                          ("91", ("x", "y"))):
            wf[n]["inputs"]["x"] = cx
            wf[n]["inputs"]["y"] = cy
            if "width" in campos:
                wf[n]["inputs"]["width"] = cw
                wf[n]["inputs"]["height"] = ch
        wf["90"]["inputs"]["width"] = cw
        wf["90"]["inputs"]["height"] = ch
        # hd2/hd3: the upscaled crop follows the crop aspect, not a square
        if "93" in wf:
            dw, dh = scale_to(cw, ch, DETAIL_RES2)
            for n in ("82", "84"):
                wf[n]["inputs"]["width"] = dw
                wf[n]["inputs"]["height"] = dh
            wf["93"]["inputs"]["text"] = (
                f"masterpiece, best quality, solo, 1girl, {IDENTITY}, {pose}, "
                "(detailed face:1.2), detailed eyes, detailed hands, detailed skin, "
                "detailed fabric folds, sharp focus, high detail")
    return wf


def outputs(res):
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
    return find(res) or []


async def main() -> int:
    p = argparse.ArgumentParser()
    add_flags(p)
    p.add_argument("--frames", type=int, default=8)
    p.add_argument("--denoise", type=float, default=0.85)
    p.add_argument("--size", type=int, default=560)
    p.add_argument("--y", type=int, default=400)
    p.add_argument("--x-start", type=int, default=20)
    p.add_argument("--x-end", type=int, default=460)
    p.add_argument("--seed-scene", type=int, default=777001)
    p.add_argument("--seed-character", type=int, default=777002)
    p.add_argument("--seed-fusion", type=int, default=777003)
    p.add_argument("--out", default="output/running")
    p.add_argument("--timeout", type=float, default=900.0)
    a = p.parse_args()

    base = from_flags(a)
    dst = registrar_run(ROOT / a.out, a, {"frames": a.frames, "denoise": a.denoise})

    n = a.frames
    paso = (a.x_end - a.x_start) / max(1, n - 1)

    cli = Serverless(api_key=api_key())
    try:
        ep = await cli.get_endpoint(name=ENDPOINT)
        for i in range(n):
            dest = dst / f"run_{i+1:02d}.png"
            if dest.is_file():
                print(f"FRAME {i+1:02d} already exists", flush=True)
                continue
            x = int(a.x_start + paso * i)
            pose = CYCLE[i % len(CYCLE)]
            print(f"FRAME {i+1:02d}/{n} x={x}", flush=True)
            wf = build(base, a, x, pose)
            res = await ep.request(
                "/generate/sync",
                {"input": {"request_id": str(uuid.uuid4()), "workflow_json": wf}},
                cost=100, timeout=a.timeout)
            outs = outputs(res)
            fin = [o for o in outs if o.get("filename", "").startswith("f_")] \
                  or [o for o in outs if o.get("filename", "").startswith("c_")]
            if not fin:
                print(f"FRAME {i+1:02d} NO OUTPUT: {json.dumps(res)[:200]}", flush=True)
                continue
            urllib.request.urlretrieve(fin[0]["url"], dest)
            print(f"FRAME {i+1:02d} OK", flush=True)
            await asyncio.sleep(2)
    finally:
        await cli.close()

    try:
        from PIL import Image
        ims = [Image.open(dst / f"run_{i+1:02d}.png").convert("RGB")
               for i in range(n) if (dst / f"run_{i+1:02d}.png").is_file()]
        if ims:
            cols = 4
            rows = (len(ims) + cols - 1) // cols
            T = 256
            sheet = Image.new("RGB", (T*cols, T*rows), (255, 255, 255))
            for k, im in enumerate(ims):
                sheet.paste(im.resize((T, T), Image.LANCZOS), ((k % cols)*T, (k//cols)*T))
            sheet.save(dst / "run_sheet.png")
            print(f"SHEET run_sheet.png with {len(ims)} frames", flush=True)
            ims[0].save(dst / "run.gif", save_all=True, append_images=ims[1:],
                        duration=140, loop=0, optimize=True)
            print("GIF run.gif", flush=True)
    except ImportError:
        pass
    print("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
