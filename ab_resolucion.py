#!/usr/bin/env python3
"""A/B de las palancas de resolucion del personaje.

Barre las combinaciones de --vertical / --lienzo / --upscale (y --bbox cuando
este implementado) con las MISMAS seeds, y monta una hoja de comparacion con el
compuesto final de cada una.

    python ab_resolucion.py                      # las 8 combinaciones
    python ab_resolucion.py --combos base,v,vl   # solo algunas
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

from config import ENDPOINT_NAME as ENDPOINT, api_key
import escena

HERE = Path(__file__).parent

# nombre -> flags que se pisan sobre los defaults
COMBOS: dict[str, dict] = {
    "base": {},
    "v":    {"vertical": True},
    "l":    {"lienzo": 1536},
    "u":    {"upscale": True},
    "vl":   {"vertical": True, "lienzo": 1536},
    "vu":   {"vertical": True, "upscale": True},
    "lu":   {"lienzo": 1536, "upscale": True},
    "vlu":  {"vertical": True, "lienzo": 1536, "upscale": True},
}


def args_de(combo: str, a) -> argparse.Namespace:
    """Copia los args base aplicando los toggles del combo."""
    n = argparse.Namespace(**vars(a))
    n.vertical = False
    n.lienzo = escena.LIENZO if hasattr(escena, "LIENZO") else 1024
    n.bbox = False
    n.upscale = False
    for k, v in COMBOS[combo].items():
        setattr(n, k, v)
    return n


async def una(cli, combo: str, a, dst: Path, tiempos: dict[str, float]) -> None:
    n = args_de(combo, a)
    wf = escena.construir(n)
    print(f"[{combo}] enviando ({len(wf)} nodos)...", flush=True)
    t0 = time.monotonic()
    ep = await cli.get_endpoint(name=ENDPOINT)
    res = await ep.request(
        "/generate/sync",
        {"input": {"request_id": str(uuid.uuid4()), "workflow_json": wf}},
        cost=100,
        timeout=a.timeout,
    )
    tiempos[combo] = time.monotonic() - t0
    outs = escena.salidas(res)
    if not outs:
        print(f"[{combo}] SIN SALIDAS: {json.dumps(res)[:400]}", flush=True)
        return
    for o in outs:
        pre = o.get("filename", "?").split("_")[0]      # a / b / c / h
        urllib.request.urlretrieve(o["url"], dst / f"{pre}_{combo}.png")
    print(f"[{combo}] OK {tiempos[combo]:.1f}s ({len(outs)} imagenes)", flush=True)


def hoja(dst: Path, combos: list[str], pre: str, salida: Path) -> None:
    from PIL import Image, ImageDraw

    ims = [(c, Image.open(dst / f"{pre}_{c}.png").convert("RGB"))
           for c in combos if (dst / f"{pre}_{c}.png").is_file()]
    if not ims:
        return
    h = max(im.height for _, im in ims)
    ws = [round(im.width * h / im.height) for _, im in ims]
    out = Image.new("RGB", (sum(ws), h + 22), (255, 255, 255))
    d = ImageDraw.Draw(out)
    x = 0
    for (c, im), w in zip(ims, ws):
        out.paste(im.resize((w, h), Image.LANCZOS), (x, 22))
        d.text((x + 6, 6), c, fill=(0, 0, 0))
        x += w
    out.save(salida)
    print(f"HOJA {salida.name} ({len(ims)} variantes)")


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--combos", default=",".join(COMBOS))
    p.add_argument("--escena")
    p.add_argument("--personaje")
    p.add_argument("--fusion")
    p.add_argument("--prompt-detalle")
    p.add_argument("--pose", default="dwpose")
    p.add_argument("--detalle", default="hd2")
    p.add_argument("--cara", action="store_true", default=True)
    p.add_argument("--manos", default=None)
    p.add_argument("--sin-mascara", action="store_true")
    p.add_argument("--seed-escena", type=int, default=111111)
    p.add_argument("--seed-personaje", type=int, default=222222)
    p.add_argument("--seed-fusion", type=int, default=333333)
    p.add_argument("--denoise", type=float, default=0.4)
    p.add_argument("--tam", type=int, default=640)
    p.add_argument("--x", type=int, default=200)
    p.add_argument("--y", type=int, default=380)
    p.add_argument("--out", default="ab_resolucion")
    p.add_argument("--timeout", type=float, default=1200.0)
    a = p.parse_args()

    combos = [c.strip() for c in a.combos.split(",") if c.strip()]
    for c in combos:
        if c not in COMBOS:
            sys.exit(f"combo desconocido: {c} (hay: {', '.join(COMBOS)})")

    dst = HERE / a.out
    dst.mkdir(parents=True, exist_ok=True)
    tiempos: dict[str, float] = {}
    cli = Serverless(api_key=api_key())
    try:
        for i, c in enumerate(combos):
            await una(cli, c, a, dst, tiempos)
            if i + 1 < len(combos):
                await asyncio.sleep(2)          # limite de ~1 req/s
    finally:
        await cli.close()

    print("\nTIEMPOS")
    for c, t in tiempos.items():
        print(f"  {c:5s} {t:6.1f}s")
    try:
        for pre, nombre in (("c", "fusionado"), ("h", "upscale")):
            hoja(dst, combos, pre, HERE / f"ab_resolucion_{nombre}.png")
    except ImportError:
        print("sin Pillow: no monto las hojas")
    print("FIN")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
