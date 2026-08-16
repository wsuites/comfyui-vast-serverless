#!/usr/bin/env python3
"""A/B de la segunda pasada de inpaint: hd vs hd2 vs hd3.

Manda el mismo workflow con las mismas seeds cambiando solo la variante de
detalle, cronometra cada request y monta dos hojas de comparacion: la imagen
entera y un zoom al 200% sobre la cara, que es donde se ve si la pasada sirve.

    python ab_detalle.py
    python ab_detalle.py --variantes hd,hd2,hd3 --denoise 0.85
"""
from __future__ import annotations

import argparse, asyncio, json, sys, time, urllib.request, uuid
from pathlib import Path

from vastai import Serverless

HERE = Path(__file__).parent
from config import ENDPOINT_NAME as ENDPOINT, api_key
from construir_wf import construir as arma, VARIANTES

# zonas de interes con los valores por defecto (x=200 y=380 tam=640)
ZOOMS = {"cara": (330, 400, 570, 640),
         "manos": (600, 850, 840, 1000)}


def construir(variante: str, a) -> dict:
    wf = arma(pose=a.pose, **VARIANTES[variante])
    if a.personaje:
        wf["20"]["inputs"]["text"] = a.personaje
    if a.fusion:
        wf["40"]["inputs"]["text"] = a.fusion
    wf["13"]["inputs"]["seed"] = a.seed_escena
    wf["23"]["inputs"]["seed"] = a.seed_personaje
    wf["43"]["inputs"]["seed"] = a.seed_fusion
    wf["43"]["inputs"]["denoise"] = a.denoise
    return wf


def salidas(res) -> list[dict]:
    def find(o):
        if isinstance(o, dict):
            if isinstance(o.get("output"), list) and o["output"]:
                return o["output"]
            for v in o.values():
                if (f := find(v)):
                    return f
        if isinstance(o, list):
            for v in o:
                if (f := find(v)):
                    return f
        return None
    return find(res) or []


def hojas(dst: Path, variantes: list[str]) -> None:
    from PIL import Image, ImageDraw
    ims = [(v, Image.open(dst / f"f_{v}.png").convert("RGB"))
           for v in variantes if (dst / f"f_{v}.png").is_file()]
    if not ims:
        return
    base = dst / "c_base.png"
    if base.is_file():
        ims.insert(0, ("sin pasada", Image.open(base).convert("RGB")))

    def hoja(nombre: str, recorte, escala: int) -> None:
        trozos = [(v, im.crop(recorte) if recorte else im) for v, im in ims]
        w, h = trozos[0][1].size
        w, h = w * escala, h * escala
        out = Image.new("RGB", (w * len(trozos), h + 22), (255, 255, 255))
        d = ImageDraw.Draw(out)
        for i, (v, im) in enumerate(trozos):
            out.paste(im.resize((w, h), Image.NEAREST if escala > 1 else Image.LANCZOS),
                      (i * w, 22))
            d.text((i * w + 6, 6), v, fill=(0, 0, 0))
        out.save(HERE / nombre)
        print(f"HOJA {nombre}")

    hoja("ab_detalle_full.png", None, 1)
    for nombre, caja in ZOOMS.items():
        hoja(f"ab_detalle_{nombre}.png", caja, 3)


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--pose", default="dwpose")
    p.add_argument("--personaje")
    p.add_argument("--fusion")
    p.add_argument("--zoom", help="caja x1,y1,x2,y2 extra para las hojas")
    p.add_argument("--variantes", default="hd,hd2,hd3")
    p.add_argument("--denoise", type=float, default=0.85)
    p.add_argument("--seed-escena", type=int, default=111111)
    p.add_argument("--seed-personaje", type=int, default=222222)
    p.add_argument("--seed-fusion", type=int, default=333333)
    p.add_argument("--out", default="ab_detalle")
    p.add_argument("--timeout", type=float, default=900.0)
    a = p.parse_args()

    variantes = [v.strip() for v in a.variantes.split(",") if v.strip()]
    dst = HERE / a.out
    dst.mkdir(parents=True, exist_ok=True)

    tiempos: dict[str, float] = {}
    cli = Serverless(api_key=api_key())
    try:
        ep = await cli.get_endpoint(name=ENDPOINT)
        for i, v in enumerate(variantes):
            print(f"[{v}] enviando...", flush=True)
            t0 = time.monotonic()
            res = await ep.request(
                "/generate/sync",
                {"input": {"request_id": str(uuid.uuid4()),
                           "workflow_json": construir(v, a)}},
                cost=100, timeout=a.timeout)
            tiempos[v] = time.monotonic() - t0
            outs = salidas(res)
            if not outs:
                print(f"[{v}] SIN SALIDAS: {json.dumps(res)[:400]}", flush=True)
                continue
            for o in outs:
                pre = o.get("filename", "?").split("_")[0]
                if pre == "f":
                    ruta = dst / f"f_{v}.png"
                elif pre == "g":                    # depth de MeshGraphormer
                    ruta = dst / f"g_depth_{v}.png"
                elif pre == "c":
                    ruta = dst / "c_base.png"       # identica en todas las variantes
                else:
                    continue
                urllib.request.urlretrieve(o["url"], ruta)
            print(f"[{v}] OK {tiempos[v]:.1f}s ({len(outs)} imagenes)", flush=True)
            if i + 1 < len(variantes):
                await asyncio.sleep(2)              # limite de ~1 req/s
    finally:
        await cli.close()

    print("\nTIEMPOS (incluye el arranque en frio en el primero)")
    for v, t in tiempos.items():
        print(f"  {v:5s} {t:6.1f}s")
    if a.zoom:
        ZOOMS["zoom"] = tuple(int(v) for v in a.zoom.split(","))
    try:
        hojas(dst, variantes)
    except ImportError:
        print("sin Pillow: no monto las hojas")
    print("FIN")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
