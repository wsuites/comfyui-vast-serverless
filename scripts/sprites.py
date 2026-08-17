#!/usr/bin/env python3
"""
Generates a sequence of RGBA sprites (transparent background) for animation.

Unlike correr.py, the scene is NOT baked here: every frame comes out cut
with alpha, so you can compose it over whatever background you want and move
it separately (parallax, panning, etc).

    python scripts/sprites.py --frames 8
"""
from __future__ import annotations

import argparse, asyncio, json, os, sys, urllib.request, uuid
from pathlib import Path

from vastai import Serverless

from config import ENDPOINT_NAME as ENDPOINT, ROOT, registrar_run

IDENTITY = ("(souryuu asuka langley:1.3), (asuka langley:1.2), "
            "neon genesis evangelion, long red hair, ahoge, (blue eyes:1.3), "
            "two side up, hair tubes, hair ribbons, school uniform, "
            "white shirt, red ribbon, pleated skirt, "
            "(white sneakers:1.2), (white socks:1.2)")

CYCLE = [
    "running to the right, side view, left leg forward, right arm forward",
    "running to the right, side view, mid stride, both feet off the ground",
    "running to the right, side view, right leg forward, left arm forward",
    "running to the right, side view, pushing off with back foot",
]

# the flat white background is what makes BiRefNet cut cleanly
BACKGROUND = "(white background:1.4), simple background, plain background"
NEG = ("worst quality, blurry, lowres, bad hands, bad anatomy, watermark, text, "
       "cropped, out of frame, cut off, close-up, portrait, upper body, "
       "detailed background, scenery, shadow on background, floor, ground, "
       "barefoot, bare feet, black shoes, boots, high heels, sandals")


def api_key() -> str:
    k = os.environ.get("VAST_API_KEY", "").strip()
    if k:
        return k
    for c in (Path.home()/".config"/"vastai"/"vast_api_key", Path.home()/".vast_api_key"):
        if c.is_file() and c.read_text().strip():
            return c.read_text().strip()
    sys.exit("VAST_API_KEY missing")


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
    p.add_argument("--workflow", default="workflows/wf_sprite.json")
    p.add_argument("--frames", type=int, default=8)
    p.add_argument("--seed", type=int, default=777002)
    p.add_argument("--out", default="output/sprites")
    p.add_argument("--timeout", type=float, default=900.0)
    a = p.parse_args()

    base = json.loads((ROOT / a.workflow).read_text(encoding="utf-8"))
    dst = registrar_run(ROOT / a.out, a, {"seed": a.seed, "frames": a.frames})

    cli = Serverless(api_key=api_key())
    try:
        ep = await cli.get_endpoint(name=ENDPOINT)
        for i in range(a.frames):
            dest = dst / f"sprite_{i+1:02d}.png"
            if dest.is_file():
                print(f"SPRITE {i+1:02d} already exists", flush=True)
                continue
            pose = CYCLE[i % len(CYCLE)]
            wf = json.loads(json.dumps(base))
            wf["20"]["inputs"]["text"] = (f"masterpiece, best quality, solo, 1girl, "
                                          f"{IDENTITY}, {pose}, (full body:1.3), "
                                          f"full body shot, feet visible, {BACKGROUND}")
            wf["21"]["inputs"]["text"] = NEG
            wf["23"]["inputs"]["seed"] = a.seed        # same seed = same Asuka
            if "30" in wf:                             # skeleton of this frame
                wf["30"]["inputs"]["image"] = f"pose_{i+1:02d}.png"
            print(f"SPRITE {i+1:02d}/{a.frames}", flush=True)
            res = await ep.request(
                "/generate/sync",
                {"input": {"request_id": str(uuid.uuid4()), "workflow_json": wf}},
                cost=100, timeout=a.timeout)
            outs = outputs(res)
            if not outs:
                print(f"SPRITE {i+1:02d} NO OUTPUT: {json.dumps(res)[:200]}", flush=True)
                continue
            urllib.request.urlretrieve(outs[0]["url"], dest)
            print(f"SPRITE {i+1:02d} OK", flush=True)
            await asyncio.sleep(2)
    finally:
        await cli.close()

    try:
        from PIL import Image
        paths = [dst / f"sprite_{i+1:02d}.png" for i in range(a.frames)]
        ims = [Image.open(r).convert("RGBA") for r in paths if r.is_file()]
        if not ims:
            print("DONE"); return 0
        # sheet on a checkerboard, to see the alpha
        T = 256
        cols = 4
        rows = (len(ims) + cols - 1) // cols
        def checkerboard(w, h, c=16):
            im = Image.new("RGB", (w, h), (235, 235, 235))
            for yy in range(0, h, c):
                for xx in range(0, w, c):
                    if (xx//c + yy//c) % 2:
                        im.paste((202, 202, 202), (xx, yy, min(xx+c, w), min(yy+c, h)))
            return im
        sheet = checkerboard(T*cols, T*rows)
        for k, im in enumerate(ims):
            t = im.resize((T, T), Image.LANCZOS)
            sheet.paste(t, ((k % cols)*T, (k//cols)*T), t)
        sheet.save(dst / "sprites_sheet.png")
        print(f"SHEET sprites_sheet.png with {len(ims)} sprites", flush=True)
        # GIF with transparency
        ims[0].save(dst / "sprites.gif", save_all=True, append_images=ims[1:],
                    duration=120, loop=0, disposal=2, transparency=0)
        print("GIF sprites.gif", flush=True)
        alpha = [sum(1 for v in im.getchannel("A").tobytes() if v < 10) * 100
                // (im.size[0]*im.size[1]) for im in ims]
        print("transparency per frame (%):", alpha, flush=True)
    except ImportError:
        pass
    print("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
