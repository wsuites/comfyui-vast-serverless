#!/usr/bin/env python3
"""
Secuencia de frames: un personaje corriendo de izquierda a derecha por un
escenario fijo.

Usa la configuracion que mejor resultado dio en las pruebas:
  escenario fijo + collage + mascara + DWPose + denoise alto + doble inpaint.

    python correr.py                # 8 frames
    python correr.py --frames 12
"""
from __future__ import annotations

import argparse, asyncio, json, os, sys, urllib.request, uuid
from pathlib import Path

from vastai import Serverless

HERE = Path(__file__).parent
from config import ENDPOINT_NAME as ENDPOINT
LIENZO = 1024
MARGEN = 32

IDENTIDAD = ("(souryuu asuka langley:1.3), (asuka langley:1.2), "
             "neon genesis evangelion, long red hair, ahoge, (blue eyes:1.3), "
             "two side up, hair tubes, hair ribbons, school uniform, "
             "white shirt, red ribbon, pleated skirt")

ESCENA = ("masterpiece, best quality, detailed background, empty city street, "
          "long sidewalk, buildings on both sides, afternoon light, "
          "side view, horizontal composition, no people")
NEG_ESCENA = ("1girl, person, people, character, worst quality, blurry, lowres, "
              "watermark, text")

# ciclo de carrera: se repite cada 4 frames
CICLO = [
    "running to the right, side view, left leg forward, right arm forward",
    "running to the right, side view, mid stride, both feet off the ground",
    "running to the right, side view, right leg forward, left arm forward",
    "running to the right, side view, pushing off with back foot",
]

NEG_PERSONAJE = ("worst quality, blurry, lowres, bad hands, bad anatomy, "
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
    sys.exit("falta VAST_API_KEY")


def construir(base: dict, a, x: int, pose: str) -> dict:
    wf = json.loads(json.dumps(base))
    y, tam = a.y, a.tam

    wf["10"]["inputs"]["text"] = ESCENA
    wf["11"]["inputs"]["text"] = NEG_ESCENA
    wf["20"]["inputs"]["text"] = (f"masterpiece, best quality, solo, 1girl, {IDENTIDAD}, "
                                  f"{pose}, (full body:1.3), full body shot, "
                                  "entire body visible, feet visible, simple background")
    wf["21"]["inputs"]["text"] = NEG_PERSONAJE
    wf["40"]["inputs"]["text"] = (f"masterpiece, best quality, solo, 1girl, {IDENTIDAD}, "
                                  f"{pose}, city street, sidewalk, afternoon light, "
                                  "coherent lighting, contact shadow, motion, dynamic")
    wf["41"]["inputs"]["text"] = NEG_FUSION

    wf["13"]["inputs"]["seed"] = a.seed_escena          # escenario identico siempre
    wf["23"]["inputs"]["seed"] = a.seed_personaje       # misma base de personaje
    wf["43"]["inputs"]["seed"] = a.seed_fusion
    wf["43"]["inputs"]["denoise"] = a.denoise

    for n in ("26", "28"):
        wf[n]["inputs"]["width"] = tam
        wf[n]["inputs"]["height"] = tam
    for n in ("30", "51", "77"):                        # collage, mascara, esqueleto
        if n in wf:
            wf[n]["inputs"]["x"] = x
            wf[n]["inputs"]["y"] = y
    if "75" in wf:
        wf["75"]["inputs"]["width"] = tam
        wf["75"]["inputs"]["height"] = tam

    # el recorte del doble inpaint sigue al personaje
    if "80" in wf:
        cx = max(0, x - MARGEN)
        cy = max(0, y - MARGEN)
        cw = min(LIENZO - cx, tam + 2 * MARGEN)
        ch = min(LIENZO - cy, tam + 2 * MARGEN)
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
    return wf


def salidas(res):
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
    p.add_argument("--workflow", default="wf_v_dwpose_hd.json")
    p.add_argument("--frames", type=int, default=8)
    p.add_argument("--denoise", type=float, default=0.85)
    p.add_argument("--tam", type=int, default=560)
    p.add_argument("--y", type=int, default=400)
    p.add_argument("--x-ini", type=int, default=20)
    p.add_argument("--x-fin", type=int, default=460)
    p.add_argument("--seed-escena", type=int, default=777001)
    p.add_argument("--seed-personaje", type=int, default=777002)
    p.add_argument("--seed-fusion", type=int, default=777003)
    p.add_argument("--out", default="correr_out")
    p.add_argument("--timeout", type=float, default=900.0)
    a = p.parse_args()

    base = json.loads((HERE / a.workflow).read_text(encoding="utf-8"))
    dst = HERE / a.out
    dst.mkdir(parents=True, exist_ok=True)

    n = a.frames
    paso = (a.x_fin - a.x_ini) / max(1, n - 1)

    cli = Serverless(api_key=api_key())
    try:
        ep = await cli.get_endpoint(name=ENDPOINT)
        for i in range(n):
            destino = dst / f"run_{i+1:02d}.png"
            if destino.is_file():
                print(f"FRAME {i+1:02d} ya existe", flush=True)
                continue
            x = int(a.x_ini + paso * i)
            pose = CICLO[i % len(CICLO)]
            print(f"FRAME {i+1:02d}/{n} x={x}", flush=True)
            wf = construir(base, a, x, pose)
            res = await ep.request(
                "/generate/sync",
                {"input": {"request_id": str(uuid.uuid4()), "workflow_json": wf}},
                cost=100, timeout=a.timeout)
            outs = salidas(res)
            fin = [o for o in outs if o.get("filename", "").startswith("f_")] \
                  or [o for o in outs if o.get("filename", "").startswith("c_")]
            if not fin:
                print(f"FRAME {i+1:02d} SIN SALIDA: {json.dumps(res)[:200]}", flush=True)
                continue
            urllib.request.urlretrieve(fin[0]["url"], destino)
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
            filas = (len(ims) + cols - 1) // cols
            T = 256
            hoja = Image.new("RGB", (T*cols, T*filas), (255, 255, 255))
            for k, im in enumerate(ims):
                hoja.paste(im.resize((T, T), Image.LANCZOS), ((k % cols)*T, (k//cols)*T))
            hoja.save(HERE / "correr_hoja.png")
            print(f"HOJA correr_hoja.png con {len(ims)} frames", flush=True)
            ims[0].save(HERE / "correr.gif", save_all=True, append_images=ims[1:],
                        duration=140, loop=0, optimize=True)
            print("GIF correr.gif", flush=True)
    except ImportError:
        pass
    print("FIN")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
