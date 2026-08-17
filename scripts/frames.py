#!/usr/bin/env python3
"""
Generates a frame sequence in the same scene.

The set is fixed with seed_scene, the character with seed_character; the only
thing that changes between frames is the pose. Used to measure how much
identity is kept without the character LoRA.

    python scripts/frames.py --denoise 0.55
"""
from __future__ import annotations

import argparse, asyncio, json, os, sys, urllib.request, uuid
from pathlib import Path

from vastai import Serverless

from config import ENDPOINT_NAME as ENDPOINT, ROOT, registrar_run

# traits that define the character: identical in all frames
IDENTITY = ("solo, 1girl, long red hair, ahoge, (blue eyes:1.3), two side up, "
            "white t-shirt, pale skin, detailed face")
SCENE = ("masterpiece, best quality, detailed background, empty bedroom, "
         "unmade bed, wooden floor, soft window light, no people")
NEG_SCENE = "1girl, person, people, character, worst quality, blurry, lowres, watermark, text"
NEG_COMMON = ("worst quality, blurry, lowres, bad hands, bad anatomy, "
              "pasted, cutout, sticker, floating, watermark, text")

POSES = [
    ("sitting on the edge of the bed, legs hanging down"),
    ("lying on the bed, on her back, relaxed"),
    ("sitting on the bed, hugging her knees"),
    ("standing next to the bed, looking at viewer"),
    ("sitting on the floor, leaning against the bed"),
    ("lying on the bed, on her stomach, feet up"),
    ("standing by the window, looking outside"),
    ("sitting on the edge of the bed, leaning forward"),
]


def api_key() -> str:
    k = os.environ.get("VAST_API_KEY", "").strip()
    if k:
        return k
    for c in (Path.home()/".config"/"vastai"/"vast_api_key", Path.home()/".vast_api_key"):
        if c.is_file() and c.read_text().strip():
            return c.read_text().strip()
    sys.exit("VAST_API_KEY missing")


def build(a, pose: str) -> dict:
    wf = json.loads((ROOT / a.workflow).read_text(encoding="utf-8"))
    wf["10"]["inputs"]["text"] = SCENE
    wf["11"]["inputs"]["text"] = NEG_SCENE
    wf["20"]["inputs"]["text"] = f"masterpiece, best quality, {IDENTITY}, {pose}, simple background"
    wf["21"]["inputs"]["text"] = NEG_COMMON
    wf["40"]["inputs"]["text"] = (f"masterpiece, best quality, {IDENTITY}, {pose}, "
                                  "bedroom, soft window light, coherent lighting, contact shadow")
    wf["41"]["inputs"]["text"] = NEG_COMMON
    # set and character fixed: only the pose changes
    wf["13"]["inputs"]["seed"] = a.seed_scene
    wf["23"]["inputs"]["seed"] = a.seed_character
    wf["43"]["inputs"]["seed"] = a.seed_fusion
    wf["43"]["inputs"]["denoise"] = a.denoise
    for n in ("26", "28"):
        wf[n]["inputs"]["width"] = a.size
        wf[n]["inputs"]["height"] = a.size
    wf["30"]["inputs"]["x"] = a.x
    wf["30"]["inputs"]["y"] = a.y
    if "51" in wf:
        wf["51"]["inputs"]["x"] = a.x
        wf["51"]["inputs"]["y"] = a.y
    if "52" in wf:
        wf["52"]["inputs"]["expand"] = a.expand
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
    p.add_argument("--workflow", default="workflows/wf_escena_mask.json")
    p.add_argument("--denoise", type=float, default=0.55)
    p.add_argument("--expand", type=int, default=24)
    p.add_argument("--seed-scene", type=int, default=111111)
    p.add_argument("--seed-character", type=int, default=222222)
    p.add_argument("--seed-fusion", type=int, default=333333)
    p.add_argument("--size", type=int, default=640)
    p.add_argument("--x", type=int, default=200)
    p.add_argument("--y", type=int, default=380)
    p.add_argument("--out", default="output/frames")
    p.add_argument("--timeout", type=float, default=900.0)
    a = p.parse_args()

    dst = registrar_run(ROOT / a.out, a, {"denoise": a.denoise, "expand": a.expand})
    cli = Serverless(api_key=api_key())
    try:
        ep = await cli.get_endpoint(name=ENDPOINT)
        for i, pose in enumerate(POSES, 1):
            dest = dst / f"frame_{i:02d}.png"
            if dest.is_file():
                print(f"FRAME {i:02d} already exists, skipping", flush=True)
                continue
            print(f"FRAME {i:02d}/{len(POSES)}", flush=True)
            wf = build(a, pose)
            res = await ep.request(
                "/generate/sync",
                {"input": {"request_id": str(uuid.uuid4()), "workflow_json": wf}},
                cost=100, timeout=a.timeout)
            outs = outputs(res)
            fin = [o for o in outs if o.get("filename", "").startswith("c_")]
            if not fin:
                print(f"FRAME {i:02d} NO c_ OUTPUT: {json.dumps(res)[:200]}", flush=True)
                continue
            urllib.request.urlretrieve(fin[0]["url"], dest)
            print(f"FRAME {i:02d} OK -> {dest.name}", flush=True)
            await asyncio.sleep(2)
    finally:
        await cli.close()

    # 4x2 contact sheet
    try:
        from PIL import Image
        ims = [Image.open(dst / f"frame_{i:02d}.png").convert("RGB").resize((256, 256), Image.LANCZOS)
               for i in range(1, len(POSES) + 1) if (dst / f"frame_{i:02d}.png").is_file()]
        if ims:
            sheet = Image.new("RGB", (256 * 4, 256 * 2), (255, 255, 255))
            for k, im in enumerate(ims):
                sheet.paste(im, ((k % 4) * 256, (k // 4) * 256))
            sheet.save(dst / "frames_sheet.png")
            print(f"SHEET frames_sheet.png with {len(ims)} frames", flush=True)
    except ImportError:
        pass
    print("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
