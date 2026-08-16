#!/usr/bin/env python3
"""
Cliente del endpoint serverless "mizuki".

Manda wf.json (formato API de ComfyUI) al worker via /generate/sync y guarda
las URLs presignadas que devuelve el api-wrapper.

Uso:
    python call_endpoint.py --prompt "aetherion, solo, 1girl, ..." --seed 123
    python call_endpoint.py --workflow wf.json --no-upscale
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

from config import ENDPOINT_NAME
HERE = Path(__file__).parent

def resolve_api_key() -> str:
    """VAST_API_KEY o, si no esta, el fichero que usa la CLI de vastai.

    Ojo: el SDK evalua os.environ.get("VAST_API_KEY") como default de un
    parametro, o sea en tiempo de import. Poner la variable despues no sirve;
    hay que pasarla a Serverless(api_key=...).
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
    sys.exit("No encuentro la API key: exporta VAST_API_KEY o corre 'vastai set api-key <KEY>'")

# nodos de wf.json que parametrizamos
NODE_POSITIVE = "3"
NODE_NEGATIVE = "5"
NODE_SAMPLER = "16"
NODE_SEED = "27"
NODE_WIDTH = "39"
NODE_HEIGHT = "40"
NODE_BATCH = "41"
NODE_SAVE = "7"
NODE_FACEDETAILER = "17"
NODE_UPSCALE = "47"
NODE_RMBG = "70"          # nodo de quitado de fondo, lo insertamos si hace falta

# --remove-bg <valor> -> (class_type, nombre del modelo)
RMBG_MODELS = {
    "birefnet": ("BiRefNetRMBG", "BiRefNet-general"),
    "birefnet-hr": ("BiRefNetRMBG", "BiRefNet-HR"),
    "birefnet-portrait": ("BiRefNetRMBG", "BiRefNet-portrait"),
    "birefnet-lite": ("BiRefNetRMBG", "BiRefNet_lite"),
    "inspyrenet": ("RMBG", "INSPYRENET"),
}


def build_workflow(args) -> dict:
    wf = json.loads(Path(args.workflow).read_text(encoding="utf-8"))

    if args.prompt:
        wf[NODE_POSITIVE]["inputs"]["text"] = args.prompt
    if args.negative:
        wf[NODE_NEGATIVE]["inputs"]["text"] = args.negative

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

    # sin upscale: SaveImage cuelga directamente del FaceDetailer y se podan
    # los nodos del upscaler para que ComfyUI no los ejecute
    if args.no_upscale:
        wf[NODE_SAVE]["inputs"]["images"] = [NODE_FACEDETAILER, 0]
        wf.pop(NODE_UPSCALE, None)
        wf.pop("46", None)

    # los PreviewImage no aportan nada en serverless
    for n in ("28", "29"):
        wf.pop(n, None)

    # quitado de fondo: se intercala entre la ultima imagen y el SaveImage,
    # asi funciona igual con upscale o sin el
    if args.remove_bg:
        cls, model = RMBG_MODELS[args.remove_bg]
        origen = wf[NODE_SAVE]["inputs"]["images"]
        # Hay que mandar TODOS los opcionales: el nodo los lee del dict sin
        # .get(), asi que si falta uno revienta con "Error in image
        # processing: 'mask_blur'".
        inputs = {
            "image": origen,
            "model": model,
            "sensitivity": args.bg_sensitivity,
            "mask_blur": args.bg_blur,
            "mask_offset": args.bg_offset,
            "invert_output": False,
            "refine_foreground": args.bg_refine,
            "background": "Alpha",          # Alpha = PNG transparente
            "background_color": "#222222",
        }
        if cls == "RMBG":
            inputs["process_res"] = 1024
        wf[NODE_RMBG] = {
            "class_type": cls,
            "inputs": inputs,
            "_meta": {"title": f"Quitar fondo ({model})"},
        }
        wf[NODE_SAVE]["inputs"]["images"] = [NODE_RMBG, 0]
        print(f"quitado de fondo: {cls} / {model} (refine={args.bg_refine})",
              file=sys.stderr)

    print(f"seed={seed} size={args.width}x{args.height} batch={args.batch} "
          f"upscale={'no' if args.no_upscale else 'si'} nodos={len(wf)}",
          file=sys.stderr)
    return wf


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--workflow", default=str(HERE / "wf.json"))
    p.add_argument("--prompt")
    p.add_argument("--negative")
    p.add_argument("--seed", type=int)
    p.add_argument("--width", type=int, default=1024)
    p.add_argument("--height", type=int, default=1024)
    p.add_argument("--batch", type=int, default=1)
    p.add_argument("--steps", type=int)
    p.add_argument("--cfg", type=float)
    p.add_argument("--no-upscale", action="store_true")
    p.add_argument("--remove-bg", choices=sorted(RMBG_MODELS),
                   help="quitar el fondo con BiRefNet o InSPyReNet")
    p.add_argument("--bg-refine", action="store_true",
                   help="refinar el borde (mejor en pelo, algo mas lento)")
    p.add_argument("--bg-sensitivity", type=float, default=1.0)
    p.add_argument("--bg-blur", type=int, default=0)
    p.add_argument("--bg-offset", type=int, default=0)
    p.add_argument("--cost", type=int, default=100,
                   help="unidades de coste para el autoscaler")
    p.add_argument("--timeout", type=float, default=900.0)
    p.add_argument("--out", default=str(HERE / "last_response.json"))
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
        # Nada de get_workers() aqui: la API va limitada a ~1 req/s y en
        # llamadas encadenadas devuelve HTTP 429 antes de llegar a generar.
        result = await endpoint.request(
            "/generate/sync", payload, cost=args.cost, timeout=args.timeout
        )
    finally:
        await client.close()

    Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"respuesta completa en {args.out}", file=sys.stderr)

    # el api-wrapper devuelve URLs presignadas de S3/R2
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
