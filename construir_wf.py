#!/usr/bin/env python3
"""
Genera variantes del workflow de escena a partir de wf_escena_mask.json.

    python construir_wf.py

Produce, para cada combinacion:
    wf_v_<pose>_<detalle>.json     pose = none|openpose|dwpose
                                   detalle = sd|hd
Donde 'hd' anade una segunda pasada de inpaint recortando la zona del
personaje y ampliandola a 1024, para recuperar el detalle que se pierde
cuando la figura ocupa poca parte del lienzo.
"""
import json
from pathlib import Path

HERE = Path(__file__).parent
BASE = json.loads((HERE / "wf_escena_mask.json").read_text(encoding="utf-8"))

LIENZO = 1024
MARGEN = 32          # margen alrededor del personaje al recortar
DETALLE_RES = 1024   # a que resolucion se re-difunde el recorte


def con_pose(wf: dict, detector: str) -> dict:
    """Anade guia de pose. detector: 'openpose' o 'dwpose'."""
    clase = "OpenposePreprocessor" if detector == "openpose" else "DWPreprocessor"
    inputs = {"image": ["25", 0],          # el PERSONAJE RECORTADO, no el collage:
              "detect_hand": "enable",     # sobre fondo limpio el detector acierta mas
              "detect_body": "enable",
              "detect_face": "enable",
              "resolution": 1024}
    wf["70"] = {"class_type": clase, "inputs": inputs,
                "_meta": {"title": f"esqueleto ({detector})"}}
    # el esqueleto sale del personaje aislado (1024) y hay que colocarlo donde
    # esta el personaje en el collage: se escala y se compone sobre negro
    wf["75"] = {"class_type": "ImageScale",
                "inputs": {"image": ["70", 0], "upscale_method": "nearest-exact",
                           "width": 640, "height": 640, "crop": "disabled"},
                "_meta": {"title": "escalar esqueleto como el personaje"}}
    wf["76"] = {"class_type": "EmptyImage",
                "inputs": {"width": LIENZO, "height": LIENZO, "batch_size": 1, "color": 0},
                "_meta": {"title": "lienzo negro para el esqueleto"}}
    wf["77"] = {"class_type": "ImageCompositeMasked",
                "inputs": {"destination": ["76", 0], "source": ["75", 0],
                           "x": 200, "y": 380, "resize_source": False},
                "_meta": {"title": "esqueleto en su sitio"}}
    wf["71"] = {"class_type": "SaveImage",
                "inputs": {"filename_prefix": "e_pose", "images": ["77", 0]},
                "_meta": {"title": "SALIDA: esqueleto usado"}}
    wf["72"] = {"class_type": "ControlNetLoader",
                "inputs": {"control_net_name": "controlnet-union-sdxl-1.0.safetensors"}}
    wf["73"] = {"class_type": "SetUnionControlNetType",
                "inputs": {"control_net": ["72", 0], "type": "openpose"}}
    wf["74"] = {"class_type": "ControlNetApplyAdvanced",
                "inputs": {"positive": ["40", 0], "negative": ["41", 0],
                           "control_net": ["73", 0], "image": ["77", 0],
                           "strength": 0.85, "start_percent": 0.0,
                           "end_percent": 0.8, "vae": ["1", 2]},
                "_meta": {"title": "aplicar pose"}}
    wf["43"]["inputs"]["positive"] = ["74", 0]
    wf["43"]["inputs"]["negative"] = ["74", 1]
    return wf


def con_detalle(wf: dict) -> dict:
    """Segunda pasada: recorta al personaje, amplia a 1024 y re-difunde."""
    x, y, tam = 200, 380, 640
    cx = max(0, x - MARGEN)
    cy = max(0, y - MARGEN)
    cw = min(LIENZO - cx, tam + 2 * MARGEN)
    ch = min(LIENZO - cy, tam + 2 * MARGEN)

    wf["80"] = {"class_type": "ImageCrop",
                "inputs": {"image": ["44", 0], "width": cw, "height": ch, "x": cx, "y": cy},
                "_meta": {"title": f"recorte {cw}x{ch} del personaje"}}
    wf["81"] = {"class_type": "CropMask",
                "inputs": {"mask": ["53", 0], "x": cx, "y": cy, "width": cw, "height": ch},
                "_meta": {"title": "misma mascara, recortada"}}
    wf["82"] = {"class_type": "ImageScale",
                "inputs": {"image": ["80", 0], "upscale_method": "lanczos",
                           "width": DETALLE_RES, "height": DETALLE_RES, "crop": "disabled"},
                "_meta": {"title": f"ampliar recorte a {DETALLE_RES}"}}
    wf["83"] = {"class_type": "MaskToImage", "inputs": {"mask": ["81", 0]}}
    wf["84"] = {"class_type": "ImageScale",
                "inputs": {"image": ["83", 0], "upscale_method": "bilinear",
                           "width": DETALLE_RES, "height": DETALLE_RES, "crop": "disabled"}}
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
                "_meta": {"title": "re-difundir el recorte a alta resolucion"}}
    wf["89"] = {"class_type": "VAEDecode", "inputs": {"samples": ["88", 0], "vae": ["1", 2]}}
    wf["90"] = {"class_type": "ImageScale",
                "inputs": {"image": ["89", 0], "upscale_method": "lanczos",
                           "width": cw, "height": ch, "crop": "disabled"},
                "_meta": {"title": "devolver el recorte a su tamano"}}
    wf["91"] = {"class_type": "ImageCompositeMasked",
                "inputs": {"destination": ["44", 0], "source": ["90", 0],
                           "mask": ["81", 0], "x": cx, "y": cy, "resize_source": False},
                "_meta": {"title": "pegar el recorte mejorado"}}
    wf["92"] = {"class_type": "SaveImage",
                "inputs": {"filename_prefix": "f_detalle", "images": ["91", 0]},
                "_meta": {"title": "SALIDA: con pasada de detalle"}}
    return wf


def main() -> None:
    for pose in ("none", "openpose", "dwpose"):
        for detalle in ("sd", "hd"):
            wf = json.loads(json.dumps(BASE))       # copia profunda
            if pose != "none":
                wf = con_pose(wf, pose)
            if detalle == "hd":
                wf = con_detalle(wf)
            nombre = f"wf_v_{pose}_{detalle}.json"
            (HERE / nombre).write_text(json.dumps(wf, indent=2, ensure_ascii=False),
                                       encoding="utf-8")
            print(f"{nombre}: {len(wf)} nodos")


if __name__ == "__main__":
    main()
