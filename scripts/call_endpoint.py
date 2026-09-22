#!/usr/bin/env python3
"""
Client for the "mizuki" serverless endpoint.

Sends wf.json (ComfyUI API format) to the worker via /generate/sync and saves
the presigned URLs returned by the api-wrapper.

Usage:
    python scripts/call_endpoint.py --prompt "aetherion, solo, 1girl, ..." --seed 123
    python scripts/call_endpoint.py --workflow workflows/wf.json --no-upscale
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import uuid
from pathlib import Path

from vastai import Serverless

from config import ENDPOINT_NAME, ROOT

def resolve_api_key() -> str:
    """VAST_API_KEY or, if not set, the file used by the vastai CLI.

    Gotcha: the SDK evaluates os.environ.get("VAST_API_KEY") as the default
    of a parameter, i.e. at import time. Setting the variable afterwards does
    not work; it must be passed to Serverless(api_key=...).
    """
    key = os.environ.get("VAST_API_KEY", "").strip()
    if key:
        return key
    for candidate in (Path.home() / ".config" / "vastai" / "vast_api_key",
                      Path.home() / ".vast_api_key"):
        if candidate.is_file():
            key = candidate.read_text().strip()
            if key:
                return key
    sys.exit("API key not found: export VAST_API_KEY or run 'vastai set api-key <KEY>'")

# wf.json nodes we parameterize
NODE_POSITIVE = "3"
NODE_NEGATIVE = "5"
NODE_SAMPLER = "16"
NODE_SEED = "27"
NODE_WIDTH = "39"
NODE_HEIGHT = "40"
NODE_BATCH = "41"
NODE_SAVE = "7"
NODE_FACEDETAILER = "17"
NODE_DETAIL_POS = "42"    # prompt the FaceDetailer repaints the face with
NODE_DETAIL_NEG = "55"
NODE_UPSCALE = "47"
NODE_RMBG = "70"          # background removal node, inserted when needed
NODE_CHECKPOINT = "1"     # the only source of model/clip/vae in wf.json
NODE_LORA = "2"
NODE_CLIPSKIP = "60"
NODE_ANIMA_CLIP = "901"   # ids outside the range the exported graph uses
NODE_ANIMA_VAE = "902"
NODE_FACE_CAP = "903"     # SEGS ordered-filter hook, inserted when needed

# How many faces the detailer is allowed to repaint per image.
#
# Not a style choice, a fuse. The YOLO provider hands FaceDetailer every box
# it finds and ultralytics stops at max_det=300; on a noisy Anima frame it
# really did return "300 faces", and each one is an independent sampling pass
# inside the 900 s request budget. Nothing in FaceDetailer or in
# UltralyticsDetectorProvider caps that count - the only lever is a detailer
# hook, applied to the SEGS after detection and before the sampling loop.
FACE_CAP_DEFAULT = 6

# --remove-bg <value> -> (class_type, model name)
RMBG_MODELS = {
    "birefnet": ("BiRefNetRMBG", "BiRefNet-general"),
    "birefnet-hr": ("BiRefNetRMBG", "BiRefNet-HR"),
    "birefnet-portrait": ("BiRefNetRMBG", "BiRefNet-portrait"),
    "birefnet-lite": ("BiRefNetRMBG", "BiRefNet_lite"),
    "inspyrenet": ("RMBG", "INSPYRENET"),
}


def _rewire(wf: dict, mapping: dict[tuple[str, int], list]) -> None:
    """Repoint every link in the graph, wherever it is.

    Walking the whole graph instead of touching a known list of nodes: the
    workflow is exported from the ComfyUI UI and gains nodes over time, and a
    link left pointing at a deleted loader fails at execution with an error
    that says nothing useful.
    """
    for node in wf.values():
        for key, value in node["inputs"].items():
            if isinstance(value, list) and len(value) == 2:
                hit = mapping.get((str(value[0]), value[1]))
                if hit is not None:
                    node["inputs"][key] = list(hit)


def to_anima(wf: dict, unet: str | None = None) -> dict:
    """Swap the SDXL loading subgraph for Anima's three loaders.

    Anima is a 2B DiT: there is no CheckpointLoaderSimple that yields
    model+clip+vae together, the text encoder is Qwen3 rather than CLIP, and
    the VAE is Qwen-Image's. The style LoRA is SDXL-only and CLIPSetLastLayer
    is a CLIP concept, so both nodes are removed rather than rewired.

    Everything downstream is architecture-agnostic in principle - the face
    detector is a YOLO bbox model and the upscaler is an ESRGAN - but their
    sampling passes run on whatever model they are handed, so the sampler and
    scheduler are moved to Anima's validated pair too.
    """
    from ab_modelo import ANIMA_CLIP, ANIMA_UNET, ANIMA_VAE, ARMS

    cfg = ARMS["anima"]
    wf[NODE_CHECKPOINT] = {
        "class_type": "UNETLoader",
        "inputs": {"unet_name": unet or ANIMA_UNET, "weight_dtype": "default"},
        "_meta": {"title": "Anima UNET"},
    }
    # CLIP `type` is stable_diffusion even though the encoder is Qwen3:
    # ComfyUI reads the architecture from the state dict.
    wf[NODE_ANIMA_CLIP] = {
        "class_type": "CLIPLoader",
        "inputs": {"clip_name": ANIMA_CLIP, "type": "stable_diffusion"},
        "_meta": {"title": "Anima text encoder"},
    }
    wf[NODE_ANIMA_VAE] = {
        "class_type": "VAELoader",
        "inputs": {"vae_name": ANIMA_VAE},
        "_meta": {"title": "Anima VAE"},
    }

    clip, vae, model = [NODE_ANIMA_CLIP, 0], [NODE_ANIMA_VAE, 0], [NODE_CHECKPOINT, 0]
    _rewire(wf, {
        (NODE_CHECKPOINT, 1): clip,      # clip straight off the checkpoint
        (NODE_CHECKPOINT, 2): vae,
        (NODE_LORA, 0): model,           # model after the LoRA -> the UNET
        (NODE_LORA, 1): clip,
        (NODE_CLIPSKIP, 0): clip,        # clip after CLIPSetLastLayer
    })
    wf.pop(NODE_LORA, None)
    wf.pop(NODE_CLIPSKIP, None)

    # Sampling: the schedule that was measured for this model, on every pass
    # that runs one. No ModelSamplingSD3/AuraFlow shift node - the official
    # template has none and adding one silently changes the schedule.
    for node in wf.values():
        if "sampler_name" in node["inputs"]:
            node["inputs"]["sampler_name"] = cfg["sampler"]
            node["inputs"]["scheduler"] = cfg["scheduler"]
    return wf


def set_lora(wf: dict, strength: float) -> dict:
    """Restrength, or remove, the style LoRA of the SDXL graph.

    At 0 the node is deleted rather than set to zero: a LoraLoader at 0.0
    still loads the file and still costs the request time.
    """
    if NODE_LORA not in wf:
        return wf
    if strength > 0:
        wf[NODE_LORA]["inputs"]["strength_model"] = strength
        wf[NODE_LORA]["inputs"]["strength_clip"] = strength
        return wf
    _rewire(wf, {
        (NODE_LORA, 0): [NODE_CHECKPOINT, 0],
        (NODE_LORA, 1): [NODE_CHECKPOINT, 1],
    })
    wf.pop(NODE_LORA)
    return wf


def cap_faces(wf: dict, count: int) -> dict:
    """Keep only the `count` largest detections in the face pass.

    SEGSOrderedFilterDetailerHookProvider sorts the segments by area and takes
    a slice; FaceDetailer runs `post_detection` on the hook before it starts
    sampling, so the discarded boxes cost nothing. Largest-first is what we
    want anyway: the face that carries the frame is the big one, and the 290
    spurious 12-pixel hits are exactly what blows the budget.
    """
    if NODE_FACEDETAILER not in wf:
        return wf                      # --no-face already pruned it
    wf[NODE_FACE_CAP] = {
        "class_type": "SEGSOrderedFilterDetailerHookProvider",
        "inputs": {
            "target": "area(=w*h)",
            "order": True,             # descending: biggest faces first
            "take_start": 0,
            "take_count": count,
        },
        "_meta": {"title": f"Face cap ({count})"},
    }
    wf[NODE_FACEDETAILER]["inputs"]["detailer_hook"] = [NODE_FACE_CAP, 0]
    return wf


def build_workflow(args) -> dict:
    wf = json.loads(Path(args.workflow).read_text(encoding="utf-8"))

    if getattr(args, "family", "wai") == "anima":
        to_anima(wf, getattr(args, "anima_model", None))
    elif getattr(args, "lora", None) is not None:
        set_lora(wf, args.lora)

    if args.prompt:
        wf[NODE_POSITIVE]["inputs"]["text"] = args.prompt
    if args.negative:
        wf[NODE_NEGATIVE]["inputs"]["text"] = args.negative

    # The face pass runs its own prompt and overrides the main one inside the
    # face mask: the shipped value carries "red eyes, blushing", which repaints
    # eye colour the scene prompt asked for. Overridable per request.
    if getattr(args, "detail_prompt", None) is not None:
        wf[NODE_DETAIL_POS]["inputs"]["value"] = args.detail_prompt
    if getattr(args, "detail_negative", None) is not None:
        wf[NODE_DETAIL_NEG]["inputs"]["value"] = args.detail_negative

    seed = args.seed if args.seed is not None else random.randint(0, 2**53)
    wf[NODE_SEED]["inputs"]["value"] = seed
    wf[NODE_WIDTH]["inputs"]["value"] = args.width
    wf[NODE_HEIGHT]["inputs"]["value"] = args.height
    wf[NODE_BATCH]["inputs"]["value"] = args.batch

    if args.steps is not None:
        wf[NODE_SAMPLER]["inputs"]["steps"] = args.steps
        wf[NODE_SAMPLER]["inputs"]["end_at_step"] = args.steps
    if args.cfg is not None:
        wf[NODE_SAMPLER]["inputs"]["cfg"] = args.cfg

    # without upscale: SaveImage hangs directly off the FaceDetailer and the
    # upscaler nodes are pruned so ComfyUI does not execute them
    if args.no_upscale:
        wf[NODE_SAVE]["inputs"]["images"] = [NODE_FACEDETAILER, 0]
        wf.pop(NODE_UPSCALE, None)
        wf.pop("46", None)

    # PreviewImage nodes add nothing in serverless mode
    for n in ("28", "29"):
        wf.pop(n, None)

    # Dropping the face pass: whatever consumed the FaceDetailer output reads
    # the raw VAEDecode instead. Done after the upscale pruning above so the
    # SaveImage link it may have just moved is repointed too.
    # The cap defaults on for every caller, including the web and mizuki.py,
    # which build their args namespace by hand and never heard of the flag.
    cap = getattr(args, "face_cap", FACE_CAP_DEFAULT)
    if cap is None:
        cap = FACE_CAP_DEFAULT
    if getattr(args, "no_face", False) and NODE_FACEDETAILER in wf:
        _rewire(wf, {(NODE_FACEDETAILER, 0): ["6", 0]})
        wf.pop(NODE_FACEDETAILER)
        wf.pop("20", None)          # the YOLO provider feeding it
    elif cap > 0:
        cap_faces(wf, cap)

    # background removal: inserted between the last image and SaveImage,
    # so it works the same with or without upscale
    if args.remove_bg:
        cls, model = RMBG_MODELS[args.remove_bg]
        origen = wf[NODE_SAVE]["inputs"]["images"]
        # ALL the optional inputs must be sent: the node reads them from the
        # dict without .get(), so if one is missing it blows up with "Error
        # in image processing: 'mask_blur'".
        inputs = {
            "image": origen,
            "model": model,
            "sensitivity": args.bg_sensitivity,
            "mask_blur": args.bg_blur,
            "mask_offset": args.bg_offset,
            "invert_output": False,
            "refine_foreground": args.bg_refine,
            "background": "Alpha",          # Alpha = transparent PNG
            "background_color": "#222222",
        }
        if cls == "RMBG":
            inputs["process_res"] = 1024
        wf[NODE_RMBG] = {
            "class_type": cls,
            "inputs": inputs,
            "_meta": {"title": f"Remove background ({model})"},
        }
        wf[NODE_SAVE]["inputs"]["images"] = [NODE_RMBG, 0]
        print(f"background removal: {cls} / {model} (refine={args.bg_refine})",
              file=sys.stderr)

    face = ("off" if getattr(args, "no_face", False)
            else "uncapped" if cap <= 0 else f"<={cap}")
    print(f"seed={seed} size={args.width}x{args.height} batch={args.batch} "
          f"upscale={'no' if args.no_upscale else 'yes'} faces={face} "
          f"nodes={len(wf)}", file=sys.stderr)
    return wf


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--workflow", default=str(ROOT / "workflows" / "wf.json"))
    p.add_argument("--prompt")
    p.add_argument("--negative")
    p.add_argument("--seed", type=int)
    p.add_argument("--width", type=int, default=1024)
    p.add_argument("--height", type=int, default=1024)
    p.add_argument("--batch", type=int, default=1)
    p.add_argument("--steps", type=int)
    p.add_argument("--cfg", type=float)
    p.add_argument("--no-upscale", action="store_true")
    p.add_argument("--family", choices=("wai", "anima"), default="wai",
                   help="model family of the scene graph; anima swaps the "
                        "checkpoint loader for its three separate loaders")
    p.add_argument("--anima-model", help="UNET file, with --family anima")
    p.add_argument("--lora", type=float,
                   help="style LoRA strength (wai only); 0 removes the node")
    p.add_argument("--detail-prompt",
                   help="prompt for the FaceDetailer pass (node 42); the "
                        "shipped default hardcodes red eyes")
    p.add_argument("--detail-negative", help="negative for the face pass")
    p.add_argument("--no-face", action="store_true",
                   help="skip the FaceDetailer pass")
    p.add_argument("--face-cap", type=int, default=FACE_CAP_DEFAULT,
                   metavar="N",
                   help=f"repaint at most N faces, largest first "
                        f"(default {FACE_CAP_DEFAULT}); 0 lifts the cap and "
                        f"lets YOLO return up to its own max_det of 300")
    p.add_argument("--remove-bg", choices=sorted(RMBG_MODELS),
                   help="remove background with BiRefNet or InSPyReNet")
    p.add_argument("--bg-refine", action="store_true",
                   help="refine the edge (better on hair, a bit slower)")
    p.add_argument("--bg-sensitivity", type=float, default=1.0)
    p.add_argument("--bg-blur", type=int, default=0)
    p.add_argument("--bg-offset", type=int, default=0)
    p.add_argument("--cost", type=int, default=100,
                   help="cost units for the autoscaler")
    p.add_argument("--timeout", type=float, default=900.0)
    p.add_argument("--out", default=str(ROOT / "output" / "general" / "last_response.json"))
    args = p.parse_args()

    workflow = build_workflow(args)
    payload = {
        "input": {
            "request_id": str(uuid.uuid4()),
            "workflow_json": workflow,
        }
    }

    client = Serverless(api_key=resolve_api_key())
    try:
        endpoint = await client.get_endpoint(name=ENDPOINT_NAME)
        # No get_workers() here: the API is limited to ~1 req/s and chained
        # calls return HTTP 429 before even reaching generation.
        result = await endpoint.request(
            "/generate/sync", payload, cost=args.cost, timeout=args.timeout
        )
    finally:
        await client.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"full response in {out}", file=sys.stderr)

    # the api-wrapper returns presigned S3/R2 URLs
    blob = json.dumps(result)
    if "http" in blob:
        for key in ("images", "output", "urls", "assets"):
            if isinstance(result, dict) and key in result:
                print(json.dumps(result[key], indent=2))
                break
        else:
            print(json.dumps(result, indent=2)[:4000])
    else:
        print(json.dumps(result, indent=2)[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
