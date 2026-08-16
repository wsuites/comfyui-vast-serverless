#!/usr/bin/env python3
"""
Genera una secuencia de sprites RGBA (fondo transparente) para animar.

A diferencia de correr.py, aqui NO se hornea el escenario: cada frame sale
recortado con alfa, asi que puedes componerlo sobre el fondo que quieras y
moverlo por separado (parallax, paneo, etc).

    python sprites.py --frames 8
"""
from __future__ import annotations

import argparse, asyncio, json, os, sys, urllib.request, uuid
from pathlib import Path

from vastai import Serverless

HERE = Path(__file__).parent
from config import ENDPOINT_NAME as ENDPOINT

IDENTIDAD = ("(souryuu asuka langley:1.3), (asuka langley:1.2), "
             "neon genesis evangelion, long red hair, ahoge, (blue eyes:1.3), "
             "two side up, hair tubes, hair ribbons, school uniform, "
             "white shirt, red ribbon, pleated skirt, "
             "(white sneakers:1.2), (white socks:1.2)")

CICLO = [
    "running to the right, side view, left leg forward, right arm forward",
    "running to the right, side view, mid stride, both feet off the ground",
    "running to the right, side view, right leg forward, left arm forward",
    "running to the right, side view, pushing off with back foot",
]

# el fondo blanco liso es lo que hace que BiRefNet recorte limpio
FONDO = "(white background:1.4), simple background, plain background"
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
    sys.exit("falta VAST_API_KEY")


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
    p.add_argument("--workflow", default="wf_sprite.json")
    p.add_argument("--frames", type=int, default=8)
    p.add_argument("--seed", type=int, default=777002)
    p.add_argument("--out", default="sprites_out")
    p.add_argument("--timeout", type=float, default=900.0)
    a = p.parse_args()

    base = json.loads((HERE / a.workflow).read_text(encoding="utf-8"))
    dst = HERE / a.out
    dst.mkdir(parents=True, exist_ok=True)

    cli = Serverless(api_key=api_key())
    try:
        ep = await cli.get_endpoint(name=ENDPOINT)
        for i in range(a.frames):
            destino = dst / f"sprite_{i+1:02d}.png"
            if destino.is_file():
                print(f"SPRITE {i+1:02d} ya existe", flush=True)
                continue
            pose = CICLO[i % len(CICLO)]
            wf = json.loads(json.dumps(base))
            wf["20"]["inputs"]["text"] = (f"masterpiece, best quality, solo, 1girl, "
                                          f"{IDENTIDAD}, {pose}, (full body:1.3), "
                                          f"full body shot, feet visible, {FONDO}")
            wf["21"]["inputs"]["text"] = NEG
            wf["23"]["inputs"]["seed"] = a.seed        # misma seed = misma Asuka
            if "30" in wf:                             # esqueleto de este frame
                wf["30"]["inputs"]["image"] = f"pose_{i+1:02d}.png"
            print(f"SPRITE {i+1:02d}/{a.frames}", flush=True)
            res = await ep.request(
                "/generate/sync",
                {"input": {"request_id": str(uuid.uuid4()), "workflow_json": wf}},
                cost=100, timeout=a.timeout)
            outs = salidas(res)
            if not outs:
                print(f"SPRITE {i+1:02d} SIN SALIDA: {json.dumps(res)[:200]}", flush=True)
                continue
            urllib.request.urlretrieve(outs[0]["url"], destino)
            print(f"SPRITE {i+1:02d} OK", flush=True)
            await asyncio.sleep(2)
    finally:
        await cli.close()

    try:
        from PIL import Image
        rutas = [dst / f"sprite_{i+1:02d}.png" for i in range(a.frames)]
        ims = [Image.open(r).convert("RGBA") for r in rutas if r.is_file()]
        if not ims:
            print("FIN"); return 0
        # hoja sobre tablero, para ver el alfa
        T = 256
        cols = 4
        filas = (len(ims) + cols - 1) // cols
        def tablero(w, h, c=16):
            im = Image.new("RGB", (w, h), (235, 235, 235))
            for yy in range(0, h, c):
                for xx in range(0, w, c):
                    if (xx//c + yy//c) % 2:
                        im.paste((202, 202, 202), (xx, yy, min(xx+c, w), min(yy+c, h)))
            return im
        hoja = tablero(T*cols, T*filas)
        for k, im in enumerate(ims):
            t = im.resize((T, T), Image.LANCZOS)
            hoja.paste(t, ((k % cols)*T, (k//cols)*T), t)
        hoja.save(HERE / "sprites_hoja.png")
        print(f"HOJA sprites_hoja.png con {len(ims)} sprites", flush=True)
        # GIF con transparencia
        ims[0].save(HERE / "sprites.gif", save_all=True, append_images=ims[1:],
                    duration=120, loop=0, disposal=2, transparency=0)
        print("GIF sprites.gif", flush=True)
        alfa = [sum(1 for v in im.getchannel("A").tobytes() if v < 10) * 100
                // (im.size[0]*im.size[1]) for im in ims]
        print("transparencia por frame (%):", alfa, flush=True)
    except ImportError:
        pass
    print("FIN")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
