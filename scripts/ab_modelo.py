#!/usr/bin/env python3
"""A/B of the base model and of the sampling schedule.

Bare text2img: no ControlNet, no collage, no detail pass, no upscale. Nothing
that crosses architectures, so the only thing being measured is the model and
its schedule. Every arm runs the SAME seeds.

Two questions at once:

  1. WAI Illustrious (SDXL) vs Anima Aesthetic (DiT 2B on Cosmos-Predict2).
     Different architectures: separate loaders, own sampler/scheduler/cfg and
     own prompt format. They are NOT comparable image to image; the 10 seeds
     are there to cover variance, not to pair frames.

  2. Whether the beta schedule buys back the skin texture the style LoRA
     flattens (docs/levers-and-dead-ends.md: 4.78 with the LoRA at 0.5, 5.97
     with it at 0.0). A beta-distributed schedule packs more steps into the
     low-noise tail, which is where surface detail gets resolved.

    python scripts/ab_modelo.py                        # all arms, 10 seeds
    python scripts/ab_modelo.py --arms wai,wai_beta57  # just the schedule
    python scripts/ab_modelo.py --seeds 3              # quick probe
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
from escena import outputs

# --- models -------------------------------------------------------------------
WAI_CKPT = "waiIllustriousSDXL_v170.safetensors"
STYLE_LORA = "stuffy_ai_style_ilxl_v2_goofy.safetensors"

# Confirmed against circlestone-labs/Anima split_files (4.18 GB each). Aesthetic
# and not Base on purpose: Base is the unrefined pretrained checkpoint, and WAI
# is a curated merge. Base vs WAI would be a rigged fight; the honest comparison
# is curated vs curated. v1.1 and v1.0b (LoRAs-only) exist if you want to move it.
ANIMA_UNET = "anima-aesthetic-v1.0.safetensors"   # models/diffusion_models/
ANIMA_CLIP = "qwen_3_06b_base.safetensors"        # models/text_encoders/
ANIMA_VAE = "qwen_image_vae.safetensors"          # models/vae/

# --- prompts ------------------------------------------------------------------
# NOT the same string on both sides, and that is the point: "same prompt" is not
# literally the same text when the encoders are different. WAI wants Illustrious
# quality tags; Anima wants lowercase danbooru with spaces, and its own card
# advises AGAINST score_* on the Aesthetic variant. Same semantic content,
# formatted for each one.
SUBJECT = ("1girl, solo, upper body, long red hair, blue eyes, white shirt, "
           "soft window light, looking at viewer, detailed skin, sharp focus")

PROMPTS = {
    "wai": {
        "pos": f"masterpiece, best quality, amazing quality, very aesthetic, {SUBJECT}",
        "neg": ("worst quality, blur, low resolution, low quality, worst aesthetic, "
                "old, early, blurry, lowres, signature, artist name, watermark"),
    },
    "anima": {
        "pos": f"masterpiece, best quality, safe, {SUBJECT}",
        "neg": ("worst quality, low quality, blurry, jpeg artifacts, "
                "chromatic aberration"),
    },
}

# --- arms ---------------------------------------------------------------------
# family: which loader stack and which prompt format
# beta57: (alpha, beta) -> forces the SamplerCustom path instead of KSampler
ARMS: dict[str, dict] = {
    # today's baseline, as wf.json runs it
    "wai": {"family": "wai", "lora": 0.5, "sampler": "dpmpp_2m",
            "scheduler": "karras", "steps": 28, "cfg": 5.5},
    # the measured texture ceiling: same thing with the style LoRA off
    "wai_nolora": {"family": "wai", "lora": 0.0, "sampler": "dpmpp_2m",
                   "scheduler": "karras", "steps": 28, "cfg": 5.5},
    # beta schedule straight off the KSampler enum (alpha/beta fixed at 0.6)
    "wai_beta": {"family": "wai", "lora": 0.5, "sampler": "dpmpp_2m",
                 "scheduler": "beta", "steps": 28, "cfg": 5.5},
    # the real beta57: alpha 0.5 / beta 0.7, which needs BetaSamplingScheduler.
    # This is a probe, not a documented recipe: beta57 is recommended for Anima,
    # and applying it to SDXL is an extrapolation. The mechanism (spend steps at
    # low noise) does not depend on the architecture, so it is worth measuring.
    "wai_beta57": {"family": "wai", "lora": 0.5, "sampler": "dpmpp_2m",
                   "beta57": (0.5, 0.7), "steps": 28, "cfg": 5.5},
    # Anima's own recommended settings, from the model card
    "anima": {"family": "anima", "sampler": "er_sde", "scheduler": "simple",
              "steps": 30, "cfg": 4.5},
}


def build(arm: str, seed: int, a) -> dict:
    """Bare text2img graph for one arm. Node ids are local to this file."""
    c = ARMS[arm]
    fam = c["family"]
    wf: dict = {}

    if fam == "wai":
        wf["1"] = {"class_type": "CheckpointLoaderSimple",
                   "inputs": {"ckpt_name": WAI_CKPT}}
        model, clip, vae = ["1", 0], ["1", 1], ["1", 2]
        if c.get("lora", 0.0) > 0:
            wf["2"] = {"class_type": "LoraLoader",
                       "inputs": {"model": model, "clip": clip,
                                  "lora_name": STYLE_LORA,
                                  "strength_model": c["lora"],
                                  "strength_clip": c["lora"]}}
            model, clip = ["2", 0], ["2", 1]
    else:
        # Three separate loaders and three separate folders: there is no
        # CheckpointLoaderSimple for Anima. The CLIP `type` is
        # stable_diffusion even though the encoder is Qwen3 -- ComfyUI detects
        # the architecture from the state dict. Verified against the official
        # template image_anima_base_v1.json.
        wf["1"] = {"class_type": "UNETLoader",
                   "inputs": {"unet_name": a.anima_model,
                              "weight_dtype": "default"}}
        wf["2"] = {"class_type": "CLIPLoader",
                   "inputs": {"clip_name": ANIMA_CLIP,
                              "type": "stable_diffusion"}}
        wf["3"] = {"class_type": "VAELoader",
                   "inputs": {"vae_name": ANIMA_VAE}}
        model, clip, vae = ["1", 0], ["2", 0], ["3", 0]
        # no ModelSamplingSD3/AuraFlow shift node: the official template has
        # none, and adding one silently changes the schedule.

    wf["10"] = {"class_type": "CLIPTextEncode",
                "inputs": {"clip": clip, "text": a.prompt or PROMPTS[fam]["pos"]}}
    wf["11"] = {"class_type": "CLIPTextEncode",
                "inputs": {"clip": clip, "text": a.negative or PROMPTS[fam]["neg"]}}
    # plain EmptyLatentImage for both: Anima's template uses it, NOT
    # EmptySD3LatentImage, despite being a DiT.
    wf["12"] = {"class_type": "EmptyLatentImage",
                "inputs": {"width": a.width, "height": a.height, "batch_size": 1}}

    if "beta57" in c:
        alpha, beta = c["beta57"]
        wf["20"] = {"class_type": "KSamplerSelect",
                    "inputs": {"sampler_name": c["sampler"]}}
        wf["21"] = {"class_type": "BetaSamplingScheduler",
                    "inputs": {"model": model, "steps": c["steps"],
                               "alpha": alpha, "beta": beta}}
        wf["22"] = {"class_type": "SamplerCustom",
                    "inputs": {"model": model, "add_noise": True,
                               "noise_seed": seed, "cfg": c["cfg"],
                               "positive": ["10", 0], "negative": ["11", 0],
                               "sampler": ["20", 0], "sigmas": ["21", 0],
                               "latent_image": ["12", 0]}}
        latent = ["22", 0]          # out 0 is the sampled latent, 1 is denoised
    else:
        wf["22"] = {"class_type": "KSampler",
                    "inputs": {"model": model, "seed": seed,
                               "steps": c["steps"], "cfg": c["cfg"],
                               "sampler_name": c["sampler"],
                               "scheduler": c["scheduler"],
                               "positive": ["10", 0], "negative": ["11", 0],
                               "latent_image": ["12", 0], "denoise": 1.0}}
        latent = ["22", 0]

    wf["30"] = {"class_type": "VAEDecode",
                "inputs": {"samples": latent, "vae": vae}}
    wf["31"] = {"class_type": "SaveImage",
                "inputs": {"filename_prefix": f"ab_{arm}", "images": ["30", 0]}}
    return wf


# --- metric -------------------------------------------------------------------
def measure(p: Path) -> dict[str, float]:
    """High-frequency energy and mean saturation.

    Texture is the absolute difference between neighbouring pixels on
    luminance, horizontal plus vertical -- the same metric behind the table in
    levers-and-dead-ends.md. The two directions are SUMMED, not averaged: that
    is what reproduces the documented scale. Checked against the run that
    produced those numbers (output/power/frontal): s121212 at LoRA 0.5 gives
    4.78 and _lora0 gives 5.97, the published pair, +25%. Averaging instead
    halves everything and silently breaks comparison with the table.

    Saturation rides along because dropping the LoRA is already documented to
    push it up ~56%: a texture gain paid for in colour is not a clean win. That
    one also reproduces on the same pair (82.6 -> 128.9).
    """
    from PIL import Image, ImageChops, ImageStat

    im = Image.open(p).convert("RGB")
    g = im.convert("L")
    w, h = g.size
    dx = ImageChops.difference(g.crop((1, 0, w, h)), g.crop((0, 0, w - 1, h)))
    dy = ImageChops.difference(g.crop((0, 1, w, h)), g.crop((0, 0, w, h - 1)))
    tex = ImageStat.Stat(dx).mean[0] + ImageStat.Stat(dy).mean[0]
    sat = ImageStat.Stat(im.convert("HSV").split()[1]).mean[0]
    return {"texture": tex, "saturation": sat}


def sheet(dst: Path, arms: list[str], seeds: list[int], out: Path) -> None:
    """Grid: one column per arm, one row per seed."""
    from PIL import Image, ImageDraw

    cell = 320
    have = [(s, a_) for s in seeds for a_ in arms
            if (dst / f"{a_}_{s}.png").is_file()]
    if not have:
        return
    W, H = cell * len(arms), 22 + cell * len(seeds)
    canvas = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(canvas)
    for col, a_ in enumerate(arms):
        d.text((col * cell + 6, 6), a_, fill=(0, 0, 0))
    for row, s in enumerate(seeds):
        for col, a_ in enumerate(arms):
            p = dst / f"{a_}_{s}.png"
            if not p.is_file():
                continue
            im = Image.open(p).convert("RGB")
            im.thumbnail((cell, cell), Image.LANCZOS)
            canvas.paste(im, (col * cell, 22 + row * cell))
    canvas.save(out)
    print(f"SHEET {out.name} ({len(have)} images)")


async def one(cli, arm: str, seed: int, a, dst: Path, rows: list) -> None:
    wf = build(arm, seed, a)
    print(f"[{arm} {seed}] sending ({len(wf)} nodes)...", flush=True)
    t0 = time.monotonic()
    ep = await cli.get_endpoint(name=ENDPOINT)
    res = await ep.request(
        "/generate/sync",
        {"input": {"request_id": str(uuid.uuid4()), "workflow_json": wf}},
        cost=100, timeout=a.timeout)
    dt = time.monotonic() - t0
    outs = outputs(res)
    if not outs:
        print(f"[{arm} {seed}] NO OUTPUTS: {json.dumps(res)[:400]}", flush=True)
        (dst / f"{arm}_{seed}_raw.json").write_text(
            json.dumps(res, indent=2, default=str), encoding="utf-8")
        return
    p = dst / f"{arm}_{seed}.png"
    urllib.request.urlretrieve(outs[0]["url"], p)
    row = {"arm": arm, "seed": seed, "seconds": round(dt, 1)}
    try:
        row.update({k: round(v, 3) for k, v in measure(p).items()})
    except ImportError:
        pass
    rows.append(row)
    extra = (f" tex {row['texture']:.2f} sat {row['saturation']:.1f}"
             if "texture" in row else "")
    print(f"[{arm} {seed}] OK {dt:.1f}s{extra}", flush=True)


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--arms", default=",".join(ARMS))
    p.add_argument("--seeds", type=int, default=10, help="how many seeds per arm")
    p.add_argument("--base-seed", type=int, default=101000)
    p.add_argument("--prompt", help="overrides BOTH arms (breaks per-family format)")
    p.add_argument("--negative")
    p.add_argument("--anima-model", default=ANIMA_UNET,
                   help="anima-aesthetic-v1.0 / v1.1 / anima-base-v1.0 ...")
    p.add_argument("--width", type=int, default=1024)
    p.add_argument("--height", type=int, default=1024)
    p.add_argument("--out", default="output/ab-model")
    p.add_argument("--timeout", type=float, default=900.0)
    a = p.parse_args()

    arms = [x.strip() for x in a.arms.split(",") if x.strip()]
    for x in arms:
        if x not in ARMS:
            sys.exit(f"unknown arm: {x} (available: {', '.join(ARMS)})")
    seeds = [a.base_seed + i for i in range(a.seeds)]

    dst = registrar_run(ROOT / a.out, a,
                        {"arms": arms, "seeds": seeds,
                         "anima_model": a.anima_model,
                         "config": {x: ARMS[x] for x in arms}})
    rows: list[dict] = []

    # A stranded worker (stopped instance whose host filled up) never rents a
    # replacement on its own, so every arm below would time out in turn. One
    # probe before a run that costs 30 renders is worth the second it takes.
    from vast_state import unstick
    fix = await asyncio.to_thread(unstick)
    if fix.get("acted"):
        print(f"[preflight] stranded worker: {fix['reason']}", flush=True)
        print(f"[preflight] {fix['detail']} - expect a cold start", flush=True)

    cli = Serverless(api_key=api_key())
    try:
        # arms OUTER, seeds INNER on purpose: the worker has 16 GB and cannot
        # hold SDXL and Anima at once. Alternating would make ComfyUI reload a
        # ~5 GB model on every single request.
        for i, arm in enumerate(arms):
            for j, s in enumerate(seeds):
                await one(cli, arm, s, a, dst, rows)
                if not (i == len(arms) - 1 and j == len(seeds) - 1):
                    await asyncio.sleep(2)        # ~1 req/s limit
    finally:
        await cli.close()

    (dst / "metrics.json").write_text(
        json.dumps(rows, indent=2), encoding="utf-8")

    if rows and "texture" in rows[0]:
        print("\n| arm | n | texture | saturation | s/img |")
        print("|---|---|---|---|---|")
        base = None
        for arm in arms:
            r = [x for x in rows if x["arm"] == arm]
            if not r:
                continue
            tex = sum(x["texture"] for x in r) / len(r)
            sat = sum(x["saturation"] for x in r) / len(r)
            sec = sum(x["seconds"] for x in r) / len(r)
            base = base if base is not None else tex
            delta = f" ({tex / base - 1:+.0%})" if base else ""
            print(f"| {arm} | {len(r)} | {tex:.2f}{delta} | {sat:.1f} | {sec:.0f} |")
        print("\nDeltas are against the first arm listed. Texture is "
              "high-frequency energy: it does not know pretty from noisy, so "
              "read it next to the sheet, never alone.")
    try:
        sheet(dst, arms, seeds, dst / "ab_model.png")
    except ImportError:
        print("Pillow missing: no sheet, no metrics")
    print(f"DONE -> {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
