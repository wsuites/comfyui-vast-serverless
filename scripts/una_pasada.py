#!/usr/bin/env python3
"""Single-pass render: character and scene generated together.

The escena.py pipeline (scene + cutout + paste) exists to put a SMALL figure
into a scene. When the figure fills the frame there is no background left to
preserve and the paste only costs: a cutout seam, no occlusion, and anatomy
generated at 832x1216 and then rescaled. One pass gives better anatomy, no
seam, and the model resolves contact with the bed by itself.

Built on workflows/wf_sprite.json because it already carries the same
checkpoint and style LoRA as the bedroom set, so the look does not shift.
Its BiRefNet cutout is dropped (we want the whole image, not a sprite) and a
2x UltimateSDUpscale is added after FaceDetailer.
"""
import argparse, asyncio, json, sys, urllib.request, uuid

import numpy as np
from PIL import Image
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from vastai import Serverless
from config import ENDPOINT_NAME as ENDPOINT, registrar_run
from escena import api_key, outputs
from construir_wf import UPSCALE_MODEL


def build(a) -> dict:
    wf = json.loads((ROOT / "workflows/wf_sprite.json").read_text(encoding="utf-8"))
    wf["20"]["inputs"]["text"] = a.prompt
    wf["21"]["inputs"]["text"] = a.negative
    wf["22"]["inputs"].update(width=a.width, height=a.height)
    wf["23"]["inputs"].update(seed=a.seed, steps=a.steps, cfg=a.cfg)
    # the style LoRA is inherited from the sprite graph; it is a prime
    # suspect for the smooth, waxy skin, so make it a lever
    wf["2"]["inputs"]["strength_model"] = a.lora
    wf["2"]["inputs"]["strength_clip"] = a.lora
    wf["17"]["inputs"]["seed"] = a.seed + 1          # FaceDetailer
    # the sprite graph ends in a cutout; we want the whole frame
    wf.pop("25", None); wf.pop("26", None)
    wf["27"] = {"class_type": "SaveImage",
                "inputs": {"filename_prefix": "a_pass", "images": ["17", 0]},
                "_meta": {"title": "OUTPUT: single pass + face"}}
    if a.upscale:
        wf["30"] = {"class_type": "UpscaleModelLoader",
                    "inputs": {"model_name": UPSCALE_MODEL}}
        wf["31"] = {"class_type": "UltimateSDUpscale",
                    "inputs": {"image": ["17", 0], "model": ["2", 0],
                               "positive": ["20", 0], "negative": ["21", 0],
                               "vae": ["1", 2], "upscale_model": ["30", 0],
                               "upscale_by": 2.0, "seed": a.seed + 2, "steps": 18,
                               "cfg": 5.0, "sampler_name": "dpmpp_2m",
                               "scheduler": "karras", "denoise": a.up_denoise,
                               "mode_type": "Linear", "tile_width": 1024,
                               "tile_height": 1024, "mask_blur": 8,
                               "tile_padding": 32, "seam_fix_mode": "None",
                               "seam_fix_denoise": 1.0, "seam_fix_width": 64,
                               "seam_fix_mask_blur": 8, "seam_fix_padding": 16,
                               "force_uniform_tiles": True, "tiled_decode": False,
                               # required: without it ComfyUI silently drops the node
                               "batch_size": 1}}
        wf["32"] = {"class_type": "SaveImage",
                    "inputs": {"filename_prefix": "h_pass", "images": ["31", 0]},
                    "_meta": {"title": "OUTPUT: upscaled 2x"}}
    return wf


def balance(path: Path, target_sat: float = 0.33) -> Path:
    """White-balance + desaturate, in post.

    Dropping the style LoRA sharpens the render but raises saturation ~56%
    and leaves an amber cast. That is a colour problem, not a detail one, so
    it is fixed here instead of by burning another render: channel gains are
    equalised over the brightest 20% of pixels (the sheets, which should be
    neutral) and saturation is pulled down to `target_sat`.
    """
    im = Image.open(path).convert("RGB")
    a = np.asarray(im, float)
    lum = a.mean(2)
    m = lum >= np.percentile(lum, 80)
    a = np.clip(a * (a[m].mean() / a[m].reshape(-1, 3).mean(0)), 0, 255)
    hsv = np.asarray(Image.fromarray(a.astype("uint8")).convert("HSV"), float)
    cur = (hsv[..., 1] / 255).mean()
    if cur > 0:
        hsv[..., 1] *= min(1.0, target_sat / cur)
    out = path.with_name(path.stem + "_bal.png")
    Image.fromarray(np.clip(hsv, 0, 255).astype("uint8"), "HSV").convert("RGB").save(out)
    return out


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--prompt", required=True)
    p.add_argument("--negative", default="worst quality, blurry, lowres, bad anatomy, "
                   "bad hands, extra fingers, extra limbs, watermark, text")
    p.add_argument("--seed", type=int, default=111111)
    p.add_argument("--width", type=int, default=1024)
    p.add_argument("--height", type=int, default=1024)
    p.add_argument("--steps", type=int, default=30)
    p.add_argument("--cfg", type=float, default=5.5)
    p.add_argument("--upscale", action="store_true")
    p.add_argument("--up-denoise", type=float, default=0.2)
    p.add_argument("--lora", type=float, default=0.5,
                   help="style LoRA strength (0 = off)")
    p.add_argument("--balance", action="store_true",
                   help="neutralise the amber cast and pull saturation back "
                        "(free, in post: with --lora 0 the render is sharper "
                        "but 56%% more saturated)")
    p.add_argument("--out", default="output/una_pasada")
    p.add_argument("--timeout", type=float, default=1600)
    a = p.parse_args()

    dst = registrar_run(ROOT / a.out, a, {})
    wf = build(a)
    cli = Serverless(api_key=api_key())
    try:
        ep = await cli.get_endpoint(name=ENDPOINT)
        res = await ep.request("/generate/sync",
                               {"input": {"request_id": str(uuid.uuid4()),
                                          "workflow_json": wf}},
                               cost=100, timeout=a.timeout)
    finally:
        await cli.close()

    outs = outputs(res)
    if not outs:
        print("NO OUTPUTS:", json.dumps(res)[:300]); return 1
    dst.mkdir(parents=True, exist_ok=True)
    for o in outs:
        name = o.get("filename", "?")
        path = dst / f"{name.split('_')[0]}.png"
        urllib.request.urlretrieve(o["url"], path)
        print(" ", path)
        if a.balance:
            print("  ", balance(path))
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
