#!/usr/bin/env python3
"""
Client for the pyworker-vast worker.

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
NODE_LATENT = "8"         # EmptyLatentImage: the txt2img latent source
NODE_VAEDECODE = "6"      # read for its `vae` link, which to_anima repoints
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
NODE_INIT_IMAGE = "904"   # img2img: LoadImage -> scale -> encode -> repeat
NODE_INIT_SCALE = "905"
NODE_INIT_ENCODE = "906"
NODE_INIT_BATCH = "907"
NODE_MASK_IMAGE = "908"   # inpaint: LoadImageMask, then crop -> sample -> paste
NODE_INIT_CROP = "909"
NODE_MASK_CROP = "910"
NODE_NOISE_MASK = "911"
NODE_PATCH_SCALE = "912"
NODE_BLEND_IMAGE = "913"
NODE_BLEND_BLUR = "914"
NODE_BLEND_MASK = "915"
NODE_BASE_BATCH = "916"
NODE_STITCH = "917"

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


def to_img2img(wf: dict, name: str, denoise: float) -> dict:
    """Feed the sampler an uploaded image instead of an empty latent.

    The only thing that changes is where the latent comes from: LoadImage ->
    ImageScale -> VAEEncode replaces EmptyLatentImage, and everything after the
    sampler - face pass, upscale, background removal - is untouched.

    Three details that are not obvious:

    * The VAE link is read off VAEDecode rather than hardcoded to the
      checkpoint, because ``to_anima`` has already repointed it at the Qwen
      VAE by the time we run. Hardcoding ``[NODE_CHECKPOINT, 2]`` here is how
      you get an img2img that works on wai and 400s on anima.
    * Width, height and batch stay wired to the same PrimitiveInt nodes the
      txt2img path uses, so ``--width``/``--batch`` keep meaning one thing.
      Only EmptyLatentImage is dropped.
    * The sampler is a KSamplerAdvanced, which has no ``denoise``. Denoise on
      that node *is* ``start_at_step``: the fraction of the schedule skipped.
      0.0 would start at the last step and return the input untouched, so the
      start is clamped to leave at least one step to actually sample.
    """
    steps = int(wf[NODE_SAMPLER]["inputs"]["steps"])
    start = int(round(steps * (1.0 - denoise)))
    wf[NODE_SAMPLER]["inputs"]["start_at_step"] = max(0, min(start, steps - 1))

    vae = wf[NODE_VAEDECODE]["inputs"]["vae"]
    wf[NODE_INIT_IMAGE] = {
        "class_type": "LoadImage",
        "inputs": {"image": name},
        "_meta": {"title": "Init image"},
    }
    # Scaled, not cropped: the caller picks width/height to match the image's
    # own aspect (see ``fit_dims``), so "disabled" never distorts in practice
    # and an explicit --width that does not match is obeyed rather than
    # silently centre-cropped.
    wf[NODE_INIT_SCALE] = {
        "class_type": "ImageScale",
        "inputs": {
            "image": [NODE_INIT_IMAGE, 0],
            "upscale_method": "lanczos",
            "width": [NODE_WIDTH, 0],
            "height": [NODE_HEIGHT, 0],
            "crop": "disabled",
        },
        "_meta": {"title": "Init image to latent size"},
    }
    wf[NODE_INIT_ENCODE] = {
        "class_type": "VAEEncode",
        "inputs": {"pixels": [NODE_INIT_SCALE, 0], "vae": vae},
        "_meta": {"title": "Encode init image"},
    }
    # One image encodes to a batch of one; --batch > 1 means the same start
    # with different noise, which is the useful reading for img2img.
    wf[NODE_INIT_BATCH] = {
        "class_type": "RepeatLatentBatch",
        "inputs": {"samples": [NODE_INIT_ENCODE, 0], "amount": [NODE_BATCH, 0]},
        "_meta": {"title": "Init latent batch"},
    }
    wf[NODE_SAMPLER]["inputs"]["latent_image"] = [NODE_INIT_BATCH, 0]
    wf.pop(NODE_LATENT, None)
    return wf


def set_init_image(wf: dict, name: str) -> dict:
    """Point the init LoadImage at the name the worker actually stored.

    ComfyUI's /upload/image is free to rename what it is given - it sanitises
    the filename and, without overwrite, appends a counter. The graph is built
    before a worker exists and the upload only happens once one does, so the
    name is patched in afterwards rather than assumed to have survived.
    """
    if NODE_INIT_IMAGE in wf:
        wf[NODE_INIT_IMAGE]["inputs"]["image"] = name
    return wf


def to_inpaint(wf: dict, mask: str, crop: dict, blur: int = 8) -> dict:
    """Repaint only the masked part of the init image, and paste it back.

    Runs on a graph ``to_img2img`` has already converted, after every other
    pass is settled, because it takes over whatever SaveImage was about to
    save. What it does is what "inpaint only masked" does elsewhere:

    * The init image and the mask are cropped to ``crop`` - the mask's
      bounding box plus context, computed by the page - before the existing
      ImageScale. So the sampler works on the region at the full ~1 MP
      budget. A hand that is 150 px wide in the source gets redrawn at
      latent resolution rather than at 150 px.
    * SetLatentNoiseMask limits sampling to the painted area. Outside it, the
      latent is the encoded original at every step.
    * The decoded patch (after the face pass, if that is on) is scaled back
      to the crop's pixel size. It is composited onto the untouched original
      through the same mask, blurred by ``blur`` px, so the seam fades
      instead of cutting. Pixels outside the mask never go through the VAE:
      the saved image is the original file with a patch on it.

    Only core nodes, so there is nothing for a worker to be missing. The
    mask is read from its red channel: the page paints white on black.
    """
    x, y = int(crop["x"]), int(crop["y"])
    w, h = int(crop["width"]), int(crop["height"])
    generated = wf[NODE_SAVE]["inputs"]["images"]

    wf[NODE_MASK_IMAGE] = {
        "class_type": "LoadImageMask",
        "inputs": {"image": mask, "channel": "red"},
        "_meta": {"title": "Inpaint mask"},
    }
    wf[NODE_INIT_CROP] = {
        "class_type": "ImageCrop",
        "inputs": {"image": [NODE_INIT_IMAGE, 0],
                   "width": w, "height": h, "x": x, "y": y},
        "_meta": {"title": "Crop to the masked region"},
    }
    wf[NODE_INIT_SCALE]["inputs"]["image"] = [NODE_INIT_CROP, 0]
    wf[NODE_MASK_CROP] = {
        "class_type": "CropMask",
        "inputs": {"mask": [NODE_MASK_IMAGE, 0],
                   "x": x, "y": y, "width": w, "height": h},
        "_meta": {"title": "Crop the mask"},
    }
    # Set after the batch repeat, not before: this way every latent in the
    # batch carries the mask, whatever RepeatLatentBatch does with one. The
    # sampler resizes the mask to the latent, so it can stay at crop size.
    wf[NODE_NOISE_MASK] = {
        "class_type": "SetLatentNoiseMask",
        "inputs": {"samples": [NODE_INIT_BATCH, 0], "mask": [NODE_MASK_CROP, 0]},
        "_meta": {"title": "Sample inside the mask only"},
    }
    wf[NODE_SAMPLER]["inputs"]["latent_image"] = [NODE_NOISE_MASK, 0]

    wf[NODE_PATCH_SCALE] = {
        "class_type": "ImageScale",
        "inputs": {"image": generated, "upscale_method": "lanczos",
                   "width": w, "height": h, "crop": "disabled"},
        "_meta": {"title": "Patch back to crop size"},
    }
    blend = [NODE_MASK_CROP, 0]
    if blur > 0:
        # Core ComfyUI has no mask blur, so the mask goes through an image and
        # comes back. ImageBlur caps the radius at 31 px.
        radius = max(1, min(int(blur), 31))
        wf[NODE_BLEND_IMAGE] = {
            "class_type": "MaskToImage",
            "inputs": {"mask": [NODE_MASK_CROP, 0]},
            "_meta": {"title": "Blend mask as image"},
        }
        wf[NODE_BLEND_BLUR] = {
            "class_type": "ImageBlur",
            "inputs": {"image": [NODE_BLEND_IMAGE, 0], "blur_radius": radius,
                       "sigma": max(0.1, min(radius / 2.0, 10.0))},
            "_meta": {"title": "Feather the seam"},
        }
        wf[NODE_BLEND_MASK] = {
            "class_type": "ImageToMask",
            "inputs": {"image": [NODE_BLEND_BLUR, 0], "channel": "red"},
            "_meta": {"title": "Blend mask"},
        }
        blend = [NODE_BLEND_MASK, 0]

    # ImageCompositeMasked resizes the source batch to the destination's
    # batch, so a single original would keep only the first of N patches.
    wf[NODE_BASE_BATCH] = {
        "class_type": "RepeatImageBatch",
        "inputs": {"image": [NODE_INIT_IMAGE, 0], "amount": [NODE_BATCH, 0]},
        "_meta": {"title": "One original per patch"},
    }
    wf[NODE_STITCH] = {
        "class_type": "ImageCompositeMasked",
        "inputs": {"destination": [NODE_BASE_BATCH, 0],
                   "source": [NODE_PATCH_SCALE, 0],
                   "x": x, "y": y, "resize_source": False, "mask": blend},
        "_meta": {"title": "Paste the patch into the original"},
    }
    wf[NODE_SAVE]["inputs"]["images"] = [NODE_STITCH, 0]
    return wf


def set_mask_image(wf: dict, name: str) -> dict:
    """``set_init_image`` for the inpaint mask: the name the worker stored."""
    if NODE_MASK_IMAGE in wf:
        wf[NODE_MASK_IMAGE]["inputs"]["image"] = name
    return wf


def fit_dims(width: int, height: int, area: int = 1024 * 1024,
             multiple: int = 64) -> tuple[int, int]:
    """The SDXL-native box closest to this image's aspect ratio.

    SDXL is trained at ~1 megapixel and degrades off it, so an init image is
    not sampled at its own resolution; it is fitted to the same pixel budget
    the txt2img path uses, keeping its aspect, snapped to a multiple of 64 so
    the latent divides cleanly.
    """
    if width <= 0 or height <= 0:
        return 1024, 1024
    scale = (area / (width * height)) ** 0.5
    w = max(multiple, round(width * scale / multiple) * multiple)
    h = max(multiple, round(height * scale / multiple) * multiple)
    return w, h


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

    # img2img, after the steps above are settled: denoise is expressed as a
    # fraction of the final step count, so it has to read the last word on it.
    # The name here is a placeholder until the image is on a worker; whoever
    # uploads it calls set_init_image with what came back.
    init = getattr(args, "init_image", None)
    if init:
        to_img2img(wf, str(init), float(getattr(args, "denoise", None) or 0.6))

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
    # The cap defaults on for every caller, including the web and console.py,
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

    # Inpaint last: it takes over whatever SaveImage ended up consuming.
    mask = getattr(args, "mask_image", None)
    if mask:
        if not init:
            raise ValueError("an inpaint mask needs an init image to paint into")
        if args.remove_bg:
            raise ValueError("inpainting pastes into an opaque original; "
                             "background removal does not apply")
        to_inpaint(wf, str(mask), dict(args.crop),
                   int(getattr(args, "mask_blur", None) or 0))

    face = ("off" if getattr(args, "no_face", False)
            else "uncapped" if cap <= 0 else f"<={cap}")
    mode = "txt2img"
    if init:
        start = wf[NODE_SAMPLER]["inputs"]["start_at_step"]
        steps_now = wf[NODE_SAMPLER]["inputs"]["steps"]
        mode = f"img2img(start={start}/{steps_now})"
        if mask:
            c = args.crop
            mode = (f"inpaint(start={start}/{steps_now} crop={c['width']}x"
                    f"{c['height']}+{c['x']}+{c['y']})")
    print(f"seed={seed} size={args.width}x{args.height} batch={args.batch} "
          f"{mode} upscale={'no' if args.no_upscale else 'yes'} faces={face} "
          f"nodes={len(wf)}", file=sys.stderr)
    return wf


def preflight() -> None:
    """Replace a worker that cannot come back, before asking it for a render.

    The web console does this before every job, which is why the same failure
    looks self-healing there and terminal here: a worker stranded on a full
    machine will accept the request and sit on it until the timeout, and the
    only visible difference from a slow render is how long you waited. It is
    one CLI call and it does nothing in the common case.

    Never fatal. If the check itself breaks, the request is still worth
    sending - the worker may well be fine.
    """
    try:
        import vast_state
        report = vast_state.unstick()
    except Exception as exc:                            # noqa: BLE001
        print(f"pre-flight check skipped: {exc}", file=sys.stderr)
        return
    if report.get("acted"):
        print(f"pre-flight: {report.get('detail')}", file=sys.stderr)


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
    p.add_argument("--no-unstick", action="store_true",
                   help="skip the pre-flight check for a stranded worker")
    p.add_argument("--out", default=str(ROOT / "output" / "general" / "last_response.json"))
    args = p.parse_args()

    workflow = build_workflow(args)
    payload = {
        "input": {
            "request_id": str(uuid.uuid4()),
            "workflow_json": workflow,
        }
    }

    if not args.no_unstick:
        preflight()

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
