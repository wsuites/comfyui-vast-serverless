#!/usr/bin/env python3
"""
Genera una secuencia de frames en un mismo escenario.

El set se fija con seed_escena, el personaje con seed_personaje; lo unico que
cambia entre frames es la pose. Sirve para medir cuanta identidad se conserva
sin LoRA del personaje.

    python frames.py --denoise 0.55
"""
from __future__ import annotations

import argparse, asyncio, json, os, sys, urllib.request, uuid
from pathlib import Path

from vastai import Serverless

HERE = Path(__file__).parent
from config import ENDPOINT_NAME as ENDPOINT

# rasgos que definen al personaje: identicos en todos los frames
IDENTIDAD = ("solo, 1girl, long red hair, ahoge, (blue eyes:1.3), two side up, "
             "white t-shirt, pale skin, detailed face")
ESCENA = ("masterpiece, best quality, detailed background, empty bedroom, "
          "unmade bed, wooden floor, soft window light, no people")
NEG_ESCENA = "1girl, person, people, character, worst quality, blurry, lowres, watermark, text"
NEG_COMUN = ("worst quality, blurry, lowres, bad hands, bad anatomy, "
             "pasted, cutout, sticker, floating, watermark, text")

POSES = [
    ("sentada en el borde", "sitting on the edge of the bed, legs hanging down"),
    ("tumbada boca arriba", "lying on the bed, on her back, relaxed"),
    ("abrazando rodillas",  "sitting on the bed, hugging her knees"),
    ("de pie junto a cama", "standing next to the bed, looking at viewer"),
    ("en el suelo apoyada", "sitting on the floor, leaning against the bed"),
    ("boca abajo",          "lying on the bed, on her stomach, feet up"),
    ("en la ventana",       "standing by the window, looking outside"),
    ("inclinada adelante",  "sitting on the edge of the bed, leaning forward"),
]


def api_key() -> str:
    k = os.environ.get("VAST_API_KEY", "").strip()
    if k:
        return k
    for c in (Path.home()/".config"/"vastai"/"vast_api_key", Path.home()/".vast_api_key"):
        if c.is_file() and c.read_text().strip():
            return c.read_text().strip()
    sys.exit("falta VAST_API_KEY")


def construir(a, pose_en: str) -> dict:
    wf = json.loads((HERE / a.workflow).read_text(encoding="utf-8"))
    wf["10"]["inputs"]["text"] = ESCENA
    wf["11"]["inputs"]["text"] = NEG_ESCENA
    wf["20"]["inputs"]["text"] = f"masterpiece, best quality, {IDENTIDAD}, {pose_en}, simple background"
    wf["21"]["inputs"]["text"] = NEG_COMUN
    wf["40"]["inputs"]["text"] = (f"masterpiece, best quality, {IDENTIDAD}, {pose_en}, "
                                  "bedroom, soft window light, coherent lighting, contact shadow")
    wf["41"]["inputs"]["text"] = NEG_COMUN
    # set y personaje fijos: solo cambia la pose
    wf["13"]["inputs"]["seed"] = a.seed_escena
    wf["23"]["inputs"]["seed"] = a.seed_personaje
    wf["43"]["inputs"]["seed"] = a.seed_fusion
    wf["43"]["inputs"]["denoise"] = a.denoise
    for n in ("26", "28"):
        wf[n]["inputs"]["width"] = a.tam
        wf[n]["inputs"]["height"] = a.tam
    wf["30"]["inputs"]["x"] = a.x
    wf["30"]["inputs"]["y"] = a.y
    if "51" in wf:
        wf["51"]["inputs"]["x"] = a.x
        wf["51"]["inputs"]["y"] = a.y
    if "52" in wf:
        wf["52"]["inputs"]["expand"] = a.expand
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
    p.add_argument("--workflow", default="wf_escena_mask.json")
    p.add_argument("--denoise", type=float, default=0.55)
    p.add_argument("--expand", type=int, default=24)
    p.add_argument("--seed-escena", type=int, default=111111)
    p.add_argument("--seed-personaje", type=int, default=222222)
    p.add_argument("--seed-fusion", type=int, default=333333)
    p.add_argument("--tam", type=int, default=640)
    p.add_argument("--x", type=int, default=200)
    p.add_argument("--y", type=int, default=380)
    p.add_argument("--out", default="frames_out")
    p.add_argument("--timeout", type=float, default=900.0)
    a = p.parse_args()

    dst = HERE / a.out
    dst.mkdir(parents=True, exist_ok=True)
    cli = Serverless(api_key=api_key())
    try:
        ep = await cli.get_endpoint(name=ENDPOINT)
        for i, (etiqueta, pose) in enumerate(POSES, 1):
            destino = dst / f"frame_{i:02d}.png"
            if destino.is_file():
                print(f"FRAME {i:02d} ya existe, salto", flush=True)
                continue
            print(f"FRAME {i:02d}/{len(POSES)} {etiqueta}", flush=True)
            wf = construir(a, pose)
            res = await ep.request(
                "/generate/sync",
                {"input": {"request_id": str(uuid.uuid4()), "workflow_json": wf}},
                cost=100, timeout=a.timeout)
            outs = salidas(res)
            fin = [o for o in outs if o.get("filename", "").startswith("c_")]
            if not fin:
                print(f"FRAME {i:02d} SIN SALIDA c_: {json.dumps(res)[:200]}", flush=True)
                continue
            urllib.request.urlretrieve(fin[0]["url"], destino)
            print(f"FRAME {i:02d} OK -> {destino.name}", flush=True)
            await asyncio.sleep(2)
    finally:
        await cli.close()

    # hoja de contactos 4x2
    try:
        from PIL import Image
        ims = [Image.open(dst / f"frame_{i:02d}.png").convert("RGB").resize((256, 256), Image.LANCZOS)
               for i in range(1, len(POSES) + 1) if (dst / f"frame_{i:02d}.png").is_file()]
        if ims:
            hoja = Image.new("RGB", (256 * 4, 256 * 2), (255, 255, 255))
            for k, im in enumerate(ims):
                hoja.paste(im, ((k % 4) * 256, (k // 4) * 256))
            hoja.save(HERE / "frames_hoja.png")
            print(f"HOJA frames_hoja.png con {len(ims)} frames", flush=True)
    except ImportError:
        pass
    print("FIN")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
