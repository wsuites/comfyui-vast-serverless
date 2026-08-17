#!/usr/bin/env python3
"""
Builds scene workflow variants from wf_escena_mask.json.

    python scripts/construir_wf.py

For every combination it produces:
    wf_v_<pose>_<detail>.json     pose = none|openpose|dwpose
                                 detail = sd|hd|hd2|hd3
Where 'hd' adds a second inpaint pass that crops the character area and
upscales it, to recover the detail lost when the figure occupies a small
part of the canvas.

    sd    no second pass
    hd    the original version: fixed 1024 crop, scene prompt, denoise 0.35
    hd2   fixes the aspect ratio, upscales to 1536 and uses its own prompt
    hd3   hd2 + FaceDetailer on the upscaled crop
"""
import json
from pathlib import Path

from config import ROOT

BASE = json.loads((ROOT / "workflows" / "wf_escena_mask.json").read_text(encoding="utf-8"))

CANVAS = 1024
MARGIN = 32            # margin around the character when cropping
DETAIL_RES = 1024      # resolution the crop is re-diffused at ('hd')
DETAIL_RES2 = 1536     # same for 'hd2'/'hd3'
DETAIL_DENOISE = 0.45

# --- Character resolution over the scene --------------------------------------
# The character is generated on its own canvas, cut out with BiRefNet and
# scaled into a box inside the scene. Resolution is lost in two different
# places, and each lever attacks one:
#
#   vertical  the character is generated on a 1024x1024 SQUARE, but a full-body
#             figure only fills a vertical strip: the rest is background pixels
#             that BiRefNet deletes later. At 832x1216 (SDXL native ratio) the
#             figure fills much more of the frame.
#   canvas    raise everything to 1536. SDXL is trained at 1024 and above that
#             it tends to duplicate elements, so this is the last resort.
#   upscale   UltimateSDUpscale on the final composite.
CHARACTER_VERTICAL = (832, 1216)   # SDXL native ratio for a standing figure
CANVAS_LARGE = 1536
BBOX_PADDING = 8      # px of air around the silhouette when cropping (--bbox)
UPSCALE_MODEL = "4x_NMKD-Siax_200k.pth"

# The second-pass prompt must not describe the scene: the crop is already
# closed on the character and asking for "bedroom, window light" makes the
# model spend capacity repainting background instead of skin, hair and hands.
DETAIL_POS = ("masterpiece, best quality, solo, 1girl, long red hair, "
              "(blue eyes:1.3), (detailed face:1.2), detailed eyes, "
              "detailed hands, detailed skin, detailed fabric folds, "
              "sharp focus, high detail")
DETAIL_NEG = ("worst quality, blurry, lowres, bad hands, bad anatomy, "
              "extra fingers, watermark, text, oversharpened, jpeg artifacts")

# In a hand bbox, "(detailed face:1.2)" is noise: the hands pass prompt must
# talk only about hands, and the negative pushes against typical failures
# (extra/fused fingers) because re-diffusing a small bbox invites them.
HANDS_POS = ("masterpiece, best quality, (detailed hands:1.3), five fingers, "
             "well-drawn hands, correct hand anatomy, sharp focus")
HANDS_NEG = ("worst quality, blurry, lowres, bad hands, mutated hands, "
             "extra fingers, fused fingers, extra digits, missing fingers, "
             "deformed hands, watermark, text")
HANDS_DENOISE = 0.30    # yolo path: detail only, no room to reinvent
MESH_DENOISE = 0.65     # MeshGraphormer path: depth holds the geometry

# The node default is 0.6 and with anime it does NOT detect: it returns a
# black depth and the pass silently does nothing (no error). Measured on the
# test image: at 0.6 it detects 0 hands, at 0.3 it detects 1. On a real photo
# it detects at 0.6 fine, so it is the style that costs it, not the setup.
MESH_DETECT_THR = 0.3


def scale_to(cw: int, ch: int, res: int) -> tuple[int, int]:
    """Dimensions of the upscaled crop, keeping the aspect ratio.

    The long side goes to `res` and the short side is rounded to a multiple
    of 8 (what the VAE asks for). The original version scaled to a fixed
    res x res, so a 704x676 crop was stretched 4% wide, re-diffused deformed
    and returned stretched: part of the detail gained was lost there.
    """
    factor = res / max(cw, ch)
    return (max(8, round(cw * factor / 8) * 8),
            max(8, round(ch * factor / 8) * 8))


def place_box(wf: dict, size: int = 640, x: int = 200, y: int = 380,
              canvas: int = CANVAS, vertical: bool = False) -> dict:
    """Only function that places the character box on the scene.

    Touches the scale nodes (26/28) and the paste nodes (30/51) and returns
    the geometry it computes, so whoever must crop afterwards (the 2nd pass)
    does not recompute it on their own and desync.
    """
    f = canvas / CANVAS
    size = max(8, round(size * f / 8) * 8)
    x, y = int(x * f), int(y * f)
    if vertical:
        pw, ph = CHARACTER_VERTICAL
        wf["22"]["inputs"]["width"] = pw
        wf["22"]["inputs"]["height"] = ph
        # the box keeps the aspect: height = size, proportional width. If
        # size x size is still forced the figure flattens and exactly what
        # is gained is lost.
        cw, ch = max(8, round(size * pw / ph / 8) * 8), size
    else:
        cw = ch = size
    for n in ("26", "28"):
        # with --bbox these two stop being ImageScale and become
        # ResizeAndPadImage, which calls the same target_width/target_height
        if wf[n]["class_type"] == "ResizeAndPadImage":
            wf[n]["inputs"]["target_width"] = cw
            wf[n]["inputs"]["target_height"] = ch
        else:
            wf[n]["inputs"]["width"] = cw
            wf[n]["inputs"]["height"] = ch
    for n in ("30", "51"):
        if n in wf:
            wf[n]["inputs"]["x"] = x
            wf[n]["inputs"]["y"] = y
    return {"x": x, "y": y, "cw": cw, "ch": ch, "canvas": canvas}


def with_vertical(wf: dict, size: int = 640) -> dict:
    """Generate the character vertically instead of on a square."""
    place_box(wf, size=size, x=wf["30"]["inputs"]["x"],
              y=wf["30"]["inputs"]["y"], vertical=True)
    return wf


def with_bbox(wf: dict) -> dict:
    """Crop the character to its bounding box before scaling into the box.

    BiRefNet returns the crop on the whole canvas, with transparent alpha
    around it. That padding travels to the destination box and eats a good
    part of the useful pixels. Cropping to the silhouette lets the figure
    use the full box.

    Needs a node that computes the bbox AT RUNTIME: the crop depends on the
    generated image, it cannot be precomputed here.

    Scheme verified against the live worker (python scripts/sondear_nodos.py):

        AILab_CropObject   (custom_nodes.ComfyUI-RMBG)
            opt: image:IMAGE, mask:MASK, padding:INT
            out: IMAGE, MASK

    It is the only candidate that wires as-is, and its pack already installs
    with FEAT_RMBG. It goes between BiRefNet (25) and the box scaling (26/28),
    and the mask must also be passed so 27/29 keep matching.

    Rejected: CropByBBoxes and ImageCropV2 require a BOUNDING_BOX type that
    only a detector (rtdetr/sdpose) or the manual canvas editor produces;
    AILab_ImageCrop crops at fixed coordinates, not to the object.

    Cropping to the silhouette also forces a change in scaling. The box
    (26/28) has a fixed aspect and the bbox does not: if it is still
    stretched with ImageScale, the character comes out deformed and exactly
    what is gained is lost. That is why 26/28 become ResizeAndPadImage
    (comfy_extras.nodes_images), which fits the figure inside the box KEEPING
    the aspect and fills the rest with black.

        ResizeAndPadImage
            req: image:IMAGE, target_width:INT, target_height:INT,
                 padding_color:white|black, interpolation:...|lanczos
            out: IMAGE

    The black fill is harmless: the mask (28) is filled the same way, and
    black = 0 = "paste nothing here", so the composite never sees the edge.
    """
    # 32 is free and falls inside the character block (26-31). NOTE: 55 is NOT
    # free, it is taken by a MaskToImage of the 2nd pass.
    if "32" in wf:
        raise SystemExit("with_bbox: node 32 already exists, find another free id")
    wf["32"] = {"class_type": "AILab_CropObject",
                "inputs": {"image": ["25", 0], "mask": ["25", 1],
                           "padding": BBOX_PADDING},
                "_meta": {"title": "crop to character silhouette"}}
    cw = wf["26"]["inputs"].get("width", 640)
    ch = wf["26"]["inputs"].get("height", 640)
    for n, fuente in (("26", ["32", 0]), ("28", ["27", 0])):
        wf[n] = {"class_type": "ResizeAndPadImage",
                 "inputs": {"image": fuente, "target_width": cw,
                            "target_height": ch, "padding_color": "black",
                            "interpolation": "lanczos"},
                 "_meta": {"title": wf[n].get("_meta", {}).get("title", "")}}
    wf["27"]["inputs"]["mask"] = ["32", 1]
    return wf


def with_canvas(wf: dict, side: int, size: int = 640, x: int = 200, y: int = 380,
                vertical: bool = False) -> dict:
    """Raise the scene/composition canvas. Scales positions and mask."""
    wf["12"]["inputs"]["width"] = side
    wf["12"]["inputs"]["height"] = side
    wf["50"]["inputs"]["width"] = side
    wf["50"]["inputs"]["height"] = side
    place_box(wf, size=size, x=x, y=y, canvas=side, vertical=vertical)
    return wf


def with_upscale(wf: dict, origen: list) -> dict:
    """UltimateSDUpscale on the final composite (2x)."""
    wf["130"] = {"class_type": "UpscaleModelLoader",
                 "inputs": {"model_name": UPSCALE_MODEL}}
    wf["131"] = {"class_type": "UltimateSDUpscale",
                 "inputs": {"image": origen, "model": ["2", 0],
                            "positive": ["40", 0], "negative": ["41", 0],
                            "vae": ["1", 2], "upscale_model": ["130", 0],
                            "upscale_by": 2.0, "seed": 888888, "steps": 18,
                            "cfg": 5.0, "sampler_name": "dpmpp_2m",
                            "scheduler": "karras", "denoise": 0.2,
                            "mode_type": "Linear", "tile_width": 1024,
                            "tile_height": 1024, "mask_blur": 8,
                            "tile_padding": 32, "seam_fix_mode": "None",
                            "seam_fix_denoise": 1.0, "seam_fix_width": 64,
                            "seam_fix_mask_blur": 8, "seam_fix_padding": 16,
                            "force_uniform_tiles": True,
                            "tiled_decode": False,
                            # required in this pack version. If missing, ComfyUI
                            # does NOT fail: it discards this output ("Output
                            # will be ignored") and returns the rest as if
                            # nothing happened.
                            "batch_size": 1},
                 "_meta": {"title": "UltimateSDUpscale 2x of the composite"}}
    wf["132"] = {"class_type": "SaveImage",
                 "inputs": {"filename_prefix": "h_upscale", "images": ["131", 0]},
                 "_meta": {"title": "OUTPUT: composite upscaled 2x"}}
    return wf


def with_pose(wf: dict, detector: str) -> dict:
    """Adds pose guidance. detector: 'openpose' or 'dwpose'."""
    clase = "OpenposePreprocessor" if detector == "openpose" else "DWPreprocessor"
    inputs = {"image": ["25", 0],          # the CROPPED CHARACTER, not the collage:
              "detect_hand": "enable",     # on a clean background the detector hits more
              "detect_body": "enable",
              "detect_face": "enable",
              "resolution": 1024}
    wf["70"] = {"class_type": clase, "inputs": inputs,
                "_meta": {"title": f"skeleton ({detector})"}}
    # the skeleton comes from the isolated character (1024) and must be placed
    # where the character is in the collage: it is scaled and composed on black
    wf["75"] = {"class_type": "ImageScale",
                "inputs": {"image": ["70", 0], "upscale_method": "nearest-exact",
                           "width": 640, "height": 640, "crop": "disabled"},
                "_meta": {"title": "scale skeleton like the character"}}
    wf["76"] = {"class_type": "EmptyImage",
                "inputs": {"width": CANVAS, "height": CANVAS, "batch_size": 1, "color": 0},
                "_meta": {"title": "black canvas for the skeleton"}}
    wf["77"] = {"class_type": "ImageCompositeMasked",
                "inputs": {"destination": ["76", 0], "source": ["75", 0],
                           "x": 200, "y": 380, "resize_source": False},
                "_meta": {"title": "skeleton in place"}}
    wf["71"] = {"class_type": "SaveImage",
                "inputs": {"filename_prefix": "e_pose", "images": ["77", 0]},
                "_meta": {"title": "OUTPUT: skeleton used"}}
    wf["72"] = {"class_type": "ControlNetLoader",
                "inputs": {"control_net_name": "controlnet-union-sdxl-1.0.safetensors"}}
    wf["73"] = {"class_type": "SetUnionControlNetType",
                "inputs": {"control_net": ["72", 0], "type": "openpose"}}
    wf["74"] = {"class_type": "ControlNetApplyAdvanced",
                "inputs": {"positive": ["40", 0], "negative": ["41", 0],
                           "control_net": ["73", 0], "image": ["77", 0],
                           "strength": 0.85, "start_percent": 0.0,
                           "end_percent": 0.8, "vae": ["1", 2]},
                "_meta": {"title": "apply pose"}}
    wf["43"]["inputs"]["positive"] = ["74", 0]
    wf["43"]["inputs"]["negative"] = ["74", 1]
    return wf


def with_detail(wf: dict) -> dict:
    """Second pass: crop to the character, upscale to 1024 and re-diffuse."""
    x, y, size = 200, 380, 640
    cx = max(0, x - MARGIN)
    cy = max(0, y - MARGIN)
    cw = min(CANVAS - cx, size + 2 * MARGIN)
    ch = min(CANVAS - cy, size + 2 * MARGIN)

    wf["80"] = {"class_type": "ImageCrop",
                "inputs": {"image": ["44", 0], "width": cw, "height": ch, "x": cx, "y": cy},
                "_meta": {"title": f"crop {cw}x{ch} of the character"}}
    wf["81"] = {"class_type": "CropMask",
                "inputs": {"mask": ["53", 0], "x": cx, "y": cy, "width": cw, "height": ch},
                "_meta": {"title": "same mask, cropped"}}
    wf["82"] = {"class_type": "ImageScale",
                "inputs": {"image": ["80", 0], "upscale_method": "lanczos",
                           "width": DETAIL_RES, "height": DETAIL_RES, "crop": "disabled"},
                "_meta": {"title": f"upscale crop to {DETAIL_RES}"}}
    wf["83"] = {"class_type": "MaskToImage", "inputs": {"mask": ["81", 0]}}
    wf["84"] = {"class_type": "ImageScale",
                "inputs": {"image": ["83", 0], "upscale_method": "bilinear",
                           "width": DETAIL_RES, "height": DETAIL_RES, "crop": "disabled"}}
    wf["85"] = {"class_type": "ImageToMask", "inputs": {"image": ["84", 0], "channel": "red"}}
    wf["86"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["82", 0], "vae": ["1", 2]}}
    wf["87"] = {"class_type": "SetLatentNoiseMask",
                "inputs": {"samples": ["86", 0], "mask": ["85", 0]}}
    wf["88"] = {"class_type": "KSampler",
                "inputs": {"model": ["2", 0], "seed": 444444, "steps": 22, "cfg": 5.0,
                           "sampler_name": "dpmpp_2m", "scheduler": "karras",
                           "denoise": 0.35,
                           "positive": ["40", 0], "negative": ["41", 0],
                           "latent_image": ["87", 0]},
                "_meta": {"title": "re-diffuse the crop at high resolution"}}
    wf["89"] = {"class_type": "VAEDecode", "inputs": {"samples": ["88", 0], "vae": ["1", 2]}}
    wf["90"] = {"class_type": "ImageScale",
                "inputs": {"image": ["89", 0], "upscale_method": "lanczos",
                           "width": cw, "height": ch, "crop": "disabled"},
                "_meta": {"title": "return the crop to its size"}}
    wf["91"] = {"class_type": "ImageCompositeMasked",
                "inputs": {"destination": ["44", 0], "source": ["90", 0],
                           "mask": ["81", 0], "x": cx, "y": cy, "resize_source": False},
                "_meta": {"title": "paste the improved crop"}}
    wf["92"] = {"class_type": "SaveImage",
                "inputs": {"filename_prefix": "f_detalle", "images": ["91", 0]},
                "_meta": {"title": "OUTPUT: with detail pass"}}
    return wf


def _hands_yolo(wf: dict, input_img: list) -> list:
    """Hands pass with yolo detector + FaceDetailer.

    FaceDetailer is not face-specific: it crops whatever bbox_detector marks.
    With 'bbox/hand_yolov8s.pt' it does exactly the same for hands. Returns
    the new image output.
    """
    wf["100"] = {"class_type": "UltralyticsDetectorProvider",
                 "inputs": {"model_name": "bbox/hand_yolov8s.pt"}}
    wf["101"] = {"class_type": "FaceDetailer",
                 "inputs": {"guide_size": 512, "guide_size_for": True, "max_size": 1024,
                            "seed": 666666, "steps": 20, "cfg": 4.0,
                            "sampler_name": "dpmpp_2m", "scheduler": "karras",
                            "denoise": HANDS_DENOISE, "feather": 5, "noise_mask": True,
                            "force_inpaint": True, "bbox_threshold": 0.4,
                            "bbox_dilation": 10, "bbox_crop_factor": 3,
                            "sam_detection_hint": "center-1", "sam_dilation": -395,
                            "sam_threshold": 0.93, "sam_bbox_expansion": 0,
                            "sam_mask_hint_threshold": 0.7,
                            "sam_mask_hint_use_negative": "False", "drop_size": 10,
                            "wildcard": "", "cycle": 1, "inpaint_model": False,
                            "noise_mask_feather": 20, "tiled_encode": False,
                            "tiled_decode": False,
                            "image": input_img, "model": ["2", 0], "clip": ["60", 0],
                            "vae": ["1", 2], "positive": ["97", 0],
                            "negative": ["98", 0], "bbox_detector": ["100", 0]},
                 "_meta": {"title": "hand detail (yolo)"}}
    return ["101", 0]


def _hands_mesh(wf: dict, input_img: list, dw: int, dh: int) -> list:
    """Hands pass with MeshGraphormer + ControlNet depth (HandRefiner).

    The node fits a 3D mesh to the hand and returns TWO things: the depth map
    of that mesh and the mask of the area. The depth goes through ControlNet
    union in depth mode, the mask delimits the re-diffusion. It is a geometry
    guide, not a fixer: the inpaint pass is still needed.
    """
    wf["110"] = {"class_type": "MeshGraphormer-DepthMapPreprocessor",
                 "inputs": {"image": input_img, "mask_bbox_padding": 30,
                            "resolution": max(dw, dh), "mask_type": "based_on_depth",
                            "mask_expand": 5, "rand_seed": 88,
                            "detect_thr": MESH_DETECT_THR,
                            "presence_thr": MESH_DETECT_THR},
                 "_meta": {"title": "3D hand mesh -> depth + mask"}}
    # the node resizes internally; we return depth and mask at the exact crop
    # size so there is no misalignment when re-diffusing
    wf["111"] = {"class_type": "ImageScale",
                 "inputs": {"image": ["110", 0], "upscale_method": "bilinear",
                            "width": dw, "height": dh, "crop": "disabled"},
                 "_meta": {"title": "depth at crop size"}}
    wf["112"] = {"class_type": "MaskToImage", "inputs": {"mask": ["110", 1]}}
    wf["113"] = {"class_type": "ImageScale",
                 "inputs": {"image": ["112", 0], "upscale_method": "bilinear",
                            "width": dw, "height": dh, "crop": "disabled"}}
    wf["114"] = {"class_type": "ImageToMask", "inputs": {"image": ["113", 0], "channel": "red"}}
    wf["115"] = {"class_type": "SaveImage",
                 "inputs": {"filename_prefix": "g_depth", "images": ["111", 0]},
                 "_meta": {"title": "OUTPUT: hand depth (diagnostics)"}}

    wf["116"] = {"class_type": "ControlNetLoader",
                 "inputs": {"control_net_name": "controlnet-union-sdxl-1.0.safetensors"}}
    wf["117"] = {"class_type": "SetUnionControlNetType",
                 "inputs": {"control_net": ["116", 0], "type": "depth"}}
    wf["118"] = {"class_type": "ControlNetApplyAdvanced",
                 "inputs": {"positive": ["97", 0], "negative": ["98", 0],
                            "control_net": ["117", 0], "image": ["111", 0],
                            "strength": 1.0, "start_percent": 0.0,
                            "end_percent": 1.0, "vae": ["1", 2]},
                 "_meta": {"title": "apply hand depth"}}
    wf["119"] = {"class_type": "VAEEncode", "inputs": {"pixels": input_img, "vae": ["1", 2]}}
    wf["120"] = {"class_type": "SetLatentNoiseMask",
                 "inputs": {"samples": ["119", 0], "mask": ["114", 0]}}
    wf["121"] = {"class_type": "KSampler",
                 "inputs": {"model": ["2", 0], "seed": 777777, "steps": 20, "cfg": 5.0,
                            "sampler_name": "dpmpp_2m", "scheduler": "karras",
                            "denoise": MESH_DENOISE,
                            "positive": ["118", 0], "negative": ["118", 1],
                            "latent_image": ["120", 0]},
                 "_meta": {"title": "re-diffuse the hand guided by depth"}}
    wf["122"] = {"class_type": "VAEDecode", "inputs": {"samples": ["121", 0], "vae": ["1", 2]}}
    return ["122", 0]


def with_detail2(wf: dict, face: bool, hands: str | None = None) -> dict:
    """Improved second pass: correct aspect, more resolution, own prompt.

    With `face`, it also adds a FaceDetailer on the already-upscaled crop,
    where it pays off: on the 1024 canvas the face measures ~90 px and the
    detector has little to work with; at 1536 on the crop it passes 300 px.
    """
    x, y, size = 200, 380, 640
    cx = max(0, x - MARGIN)
    cy = max(0, y - MARGIN)
    cw = min(CANVAS - cx, size + 2 * MARGIN)
    ch = min(CANVAS - cy, size + 2 * MARGIN)
    dw, dh = scale_to(cw, ch, DETAIL_RES2)

    wf["80"] = {"class_type": "ImageCrop",
                "inputs": {"image": ["44", 0], "width": cw, "height": ch, "x": cx, "y": cy},
                "_meta": {"title": f"crop {cw}x{ch} of the character"}}
    wf["81"] = {"class_type": "CropMask",
                "inputs": {"mask": ["53", 0], "x": cx, "y": cy, "width": cw, "height": ch},
                "_meta": {"title": "same mask, cropped"}}
    wf["82"] = {"class_type": "ImageScale",
                "inputs": {"image": ["80", 0], "upscale_method": "lanczos",
                           "width": dw, "height": dh, "crop": "disabled"},
                "_meta": {"title": f"upscale crop to {dw}x{dh} (aspect intact)"}}
    wf["83"] = {"class_type": "MaskToImage", "inputs": {"mask": ["81", 0]}}
    wf["84"] = {"class_type": "ImageScale",
                "inputs": {"image": ["83", 0], "upscale_method": "bilinear",
                           "width": dw, "height": dh, "crop": "disabled"}}
    wf["85"] = {"class_type": "ImageToMask", "inputs": {"image": ["84", 0], "channel": "red"}}
    wf["86"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["82", 0], "vae": ["1", 2]}}
    wf["87"] = {"class_type": "SetLatentNoiseMask",
                "inputs": {"samples": ["86", 0], "mask": ["85", 0]}}
    wf["93"] = {"class_type": "CLIPTextEncode",
                "inputs": {"clip": ["60", 0], "text": DETAIL_POS},
                "_meta": {"title": "crop positive (no scene)"}}
    wf["94"] = {"class_type": "CLIPTextEncode",
                "inputs": {"clip": ["60", 0], "text": DETAIL_NEG},
                "_meta": {"title": "crop negative"}}
    wf["88"] = {"class_type": "KSampler",
                "inputs": {"model": ["2", 0], "seed": 444444, "steps": 24, "cfg": 5.0,
                           "sampler_name": "dpmpp_2m", "scheduler": "karras",
                           "denoise": DETAIL_DENOISE,
                           "positive": ["93", 0], "negative": ["94", 0],
                           "latent_image": ["87", 0]},
                "_meta": {"title": "re-diffuse the crop at high resolution"}}
    wf["89"] = {"class_type": "VAEDecode", "inputs": {"samples": ["88", 0], "vae": ["1", 2]}}

    origen = ["89", 0]
    if face:
        wf["96"] = {"class_type": "UltralyticsDetectorProvider",
                    "inputs": {"model_name": "bbox/face_yolov8m.pt"}}
        wf["95"] = {"class_type": "FaceDetailer",
                    "inputs": {"guide_size": 512, "guide_size_for": True, "max_size": 1024,
                               "seed": 555555, "steps": 20, "cfg": 4.0,
                               "sampler_name": "dpmpp_2m", "scheduler": "karras",
                               "denoise": 0.4, "feather": 5, "noise_mask": True,
                               "force_inpaint": True, "bbox_threshold": 0.4,
                               "bbox_dilation": 10, "bbox_crop_factor": 3,
                               "sam_detection_hint": "center-1", "sam_dilation": -395,
                               "sam_threshold": 0.93, "sam_bbox_expansion": 0,
                               "sam_mask_hint_threshold": 0.7,
                               "sam_mask_hint_use_negative": "False", "drop_size": 10,
                               "wildcard": "", "cycle": 1, "inpaint_model": False,
                               "noise_mask_feather": 20, "tiled_encode": False,
                               "tiled_decode": False,
                               "image": ["89", 0], "model": ["2", 0], "clip": ["60", 0],
                               "vae": ["1", 2], "positive": ["93", 0],
                               "negative": ["94", 0], "bbox_detector": ["96", 0]},
                    "_meta": {"title": "FaceDetailer on the upscaled crop"}}
        origen = ["95", 0]

    if hands:
        wf["97"] = {"class_type": "CLIPTextEncode",
                    "inputs": {"clip": ["60", 0], "text": HANDS_POS},
                    "_meta": {"title": "hands positive"}}
        wf["98"] = {"class_type": "CLIPTextEncode",
                    "inputs": {"clip": ["60", 0], "text": HANDS_NEG},
                    "_meta": {"title": "hands negative"}}
        # order matters: first the mesh fixes the geometry, then the detailer
        # adds detail on an already well-built hand
        if hands in ("mesh", "both"):
            origen = _hands_mesh(wf, origen, dw, dh)
        if hands in ("yolo", "both"):
            origen = _hands_yolo(wf, origen)

    wf["90"] = {"class_type": "ImageScale",
                "inputs": {"image": origen, "upscale_method": "lanczos",
                           "width": cw, "height": ch, "crop": "disabled"},
                "_meta": {"title": "return the crop to its size"}}
    wf["91"] = {"class_type": "ImageCompositeMasked",
                "inputs": {"destination": ["44", 0], "source": ["90", 0],
                           "mask": ["81", 0], "x": cx, "y": cy, "resize_source": False},
                "_meta": {"title": "paste the improved crop"}}
    wf["92"] = {"class_type": "SaveImage",
                "inputs": {"filename_prefix": "f_detalle", "images": ["91", 0]},
                "_meta": {"title": "OUTPUT: with detail pass"}}
    return wf


def no_mask(wf: dict) -> dict:
    """Removes the mask: img2img re-diffuses the whole canvas.

    Documented as a BAD idea (it deforms the scene as soon as the denoise is
    raised), but it is a legitimate switch to reproduce the comparison. The
    nodes must be DELETED, not just disconnected: ComfyUI executes everything
    still hanging from an output.
    """
    wf["43"]["inputs"]["latent_image"] = ["42", 0]      # raw VAEEncode
    for n in ("50", "51", "52", "53", "54", "55", "56"):
        wf.pop(n, None)
    if "81" in wf:      # the 2nd pass cropped that mask: without it, no
        raise SystemExit("--no-mask is not compatible with the 2nd pass "
                         "(the crop needs the mask from node 53)")
    return wf


def build(pose: str = "dwpose", detail: str = "hd2", face: bool = True,
          hands: str | None = None, mask: bool = True,
          vertical: bool = False, canvas: int = CANVAS,
          bbox: bool = False, upscale: bool = False) -> dict:
    """Assembles the workflow by switching each feature on and off.

    pose     none | openpose | dwpose   ControlNet pose guidance
    detail   no | hd | hd2             2nd pass on the character crop
    face     bool                       FaceDetailer inside the 2nd pass
    hands    None | yolo | mesh | both  hands pass inside the 2nd pass
    mask     bool                       SetLatentNoiseMask in the fusion

    Character resolution (see the CHARACTER_VERTICAL comment):
    vertical bool    generate the character at 832x1216 instead of 1024 square
    canvas   int     side of the scene/composition (1024 by default)
    bbox     bool    crop the character to its bounding box before scaling
    upscale  bool    UltimateSDUpscale 2x on the final composite
    """
    if pose not in ("none", "openpose", "dwpose"):
        raise SystemExit(f"unknown pose: {pose}")
    if detail not in ("no", "hd", "hd2"):
        raise SystemExit(f"unknown detail: {detail}")
    if hands not in (None, "yolo", "mesh", "both"):
        raise SystemExit(f"unknown hands: {hands}")
    if detail == "no" and (face or hands):
        raise SystemExit("--face and --hands live INSIDE the 2nd pass: "
                         "they need --detail hd2")
    if detail == "hd" and (face or hands):
        raise SystemExit("--face and --hands only exist with --detail hd2 "
                         "('hd' is kept only as comparison baseline)")

    wf = json.loads(json.dumps(BASE))               # deep copy
    # canvas and vertical are resolved in one go (one writes the scale and
    # the other the aspect of the SAME box: applied separately they stomped
    # each other)
    if canvas != CANVAS or vertical:
        wf = with_canvas(wf, canvas, vertical=vertical)
    if bbox:
        wf = with_bbox(wf)
    if pose != "none":
        wf = with_pose(wf, pose)
    if detail == "hd":
        wf = with_detail(wf)
    elif detail == "hd2":
        wf = with_detail2(wf, face=face, hands=hands)
    if not mask:
        wf = no_mask(wf)
    if upscale:
        # hangs off the last existing output: 2nd pass > fusion
        wf = with_upscale(wf, ["91", 0] if "91" in wf else ["44", 0])
    return wf


# Short names used by the A/B and by the runners. Each one is a concrete
# combination of the switches above.
VARIANTS = {
    "sd":    dict(detail="no",  face=False, hands=None),
    "hd":    dict(detail="hd",  face=False, hands=None),
    "hd2":   dict(detail="hd2", face=False, hands=None),
    "hd3":   dict(detail="hd2", face=True,  hands=None),
    "hd3y":  dict(detail="hd2", face=True,  hands="yolo"),
    "hd3m":  dict(detail="hd2", face=True,  hands="mesh"),
    "hd3ym": dict(detail="hd2", face=True,  hands="both"),
}


def add_flags(p) -> None:
    """Adds the workflow switches to an ArgumentParser.

    Used by escena.py, correr.py and ab_detalle.py so the same flags mean
    the same thing everywhere.
    """
    g = p.add_argument_group("workflow switches")
    g.add_argument("--pose", default="dwpose", choices=("none", "openpose", "dwpose"),
                   help="ControlNet pose guidance (default: dwpose)")
    g.add_argument("--detail", default="hd2", choices=("no", "hd", "hd2"),
                   help="2nd pass on the character crop (default: hd2)")
    g.add_argument("--face", action="store_true", help="FaceDetailer in the 2nd pass")
    g.add_argument("--hands", choices=("yolo", "mesh", "both"),
                   help="hands pass in the 2nd pass")
    g.add_argument("--no-mask", action="store_true",
                   help="remove SetLatentNoiseMask (deforms the scene)")
    g.add_argument("--variant", choices=tuple(VARIANTS),
                   help="named shortcut; ignores --detail/--face/--hands")
    g.add_argument("--workflow", help="use this .json instead of building from flags")
    r = p.add_argument_group("character resolution")
    r.add_argument("--vertical", action="store_true",
                   help=f"generate the character at {CHARACTER_VERTICAL[0]}x{CHARACTER_VERTICAL[1]}")
    r.add_argument("--canvas", type=int, default=CANVAS,
                   help=f"scene side (default {CANVAS}; {CANVAS_LARGE} duplicates elements)")
    r.add_argument("--bbox", action="store_true",
                   help="crop the character to its bounding box")
    r.add_argument("--upscale", action="store_true",
                   help="UltimateSDUpscale 2x of the final composite")


def from_flags(a) -> dict:
    """Returns the workflow: from the --workflow file, or built from flags."""
    if getattr(a, "workflow", None):
        return json.loads((ROOT / a.workflow).read_text(encoding="utf-8"))
    opts = dict(VARIANTS[a.variant]) if getattr(a, "variant", None) else \
        dict(detail=a.detail, face=a.face, hands=a.hands)
    # the resolution levers are orthogonal to the variant: they are always
    # applied, also when --variant is used as a shortcut
    for k in ("vertical", "canvas", "bbox", "upscale"):
        opts[k] = getattr(a, k, CANVAS if k == "canvas" else False)
    return build(pose=a.pose, mask=not a.no_mask, **opts)


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(
        description="Builds the scene workflow by switching each part on/off.",
        epilog="Examples:\n"
               "  python scripts/construir_wf.py --matrix\n"
               "  python scripts/construir_wf.py --pose dwpose --detail hd2 --face "
               "--hands yolo -o my_wf.json\n"
               "  python scripts/construir_wf.py --variant hd3y -o my_wf.json",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pose", default="dwpose", choices=("none", "openpose", "dwpose"))
    p.add_argument("--detail", default="hd2", choices=("no", "hd", "hd2"))
    p.add_argument("--face", action="store_true", help="FaceDetailer in the 2nd pass")
    p.add_argument("--hands", choices=("yolo", "mesh", "both"))
    p.add_argument("--no-mask", action="store_true",
                   help="remove SetLatentNoiseMask (deforms the scene)")
    p.add_argument("--variant", choices=tuple(VARIANTS),
                   help="shortcut: applies a named combination")
    p.add_argument("-o", "--output", help="file to write the workflow to")
    p.add_argument("--matrix", action="store_true",
                   help="regenerates all wf_v_<pose>_<variant>.json")
    a = p.parse_args()

    if a.matrix:
        for pose in ("none", "openpose", "dwpose"):
            for name, opts in VARIANTS.items():
                wf = build(pose=pose, **opts)
                f = ROOT / "workflows" / f"wf_v_{pose}_{name}.json"
                f.write_text(json.dumps(wf, indent=2, ensure_ascii=False), encoding="utf-8")
                print(f"{f.name}: {len(wf)} nodes")
        return

    opts = dict(VARIANTS[a.variant]) if a.variant else \
        dict(detail=a.detail, face=a.face, hands=a.hands)
    wf = build(pose=a.pose, mask=not a.no_mask, **opts)

    texto = json.dumps(wf, indent=2, ensure_ascii=False)
    if a.output:
        (ROOT / a.output).write_text(texto, encoding="utf-8")
        activo = [f"pose={a.pose}", f"detail={opts['detail']}"]
        if opts.get("face"):
            activo.append("face")
        if opts.get("hands"):
            activo.append(f"hands={opts['hands']}")
        if a.no_mask:
            activo.append("NO mask")
        print(f"{a.output}: {len(wf)} nodes  [{', '.join(activo)}]")
    else:
        print(texto)


if __name__ == "__main__":
    main()
