#!/usr/bin/env python3
"""
Pipeline escenario -> collage -> img2img, todo en un solo workflow.

No sube imagenes al worker: el escenario y el personaje se generan, se recortan
con BiRefNet y se componen dentro del propio grafo de ComfyUI.

Devuelve 3 imagenes por ejecucion:
  a_escena     el escenario limpio (reutilizable como set fijo)
  b_collage    el pegado crudo, para comparar
  c_fusionado  tras la pasada de img2img

Uso:
    python escena.py --denoise 0.4
    python escena.py --sweep 0.25,0.35,0.45,0.55
"""
from __future__ import annotations

import argparse, asyncio, json, os, sys, urllib.request, uuid
from pathlib import Path

from vastai import Serverless

HERE = Path(__file__).parent
from config import ENDPOINT_NAME as ENDPOINT
from construir_wf import (escalado, LIENZO, MARGEN, DETALLE_RES2,
                          anadir_flags, desde_flags)


def api_key() -> str:
    k = os.environ.get("VAST_API_KEY", "").strip()
    if k:
        return k
    for c in (Path.home()/".config"/"vastai"/"vast_api_key", Path.home()/".vast_api_key"):
        if c.is_file() and c.read_text().strip():
            return c.read_text().strip()
    sys.exit("falta VAST_API_KEY")


def construir(a) -> dict:
    wf = desde_flags(a)
    if a.escena:
        wf["10"]["inputs"]["text"] = a.escena
    if a.personaje:
        wf["20"]["inputs"]["text"] = a.personaje
    if a.fusion:
        wf["40"]["inputs"]["text"] = a.fusion
    wf["13"]["inputs"]["seed"] = a.seed_escena
    wf["23"]["inputs"]["seed"] = a.seed_personaje
    wf["43"]["inputs"]["seed"] = a.seed_fusion
    wf["43"]["inputs"]["denoise"] = a.denoise
    for n in ("26", "28"):
        wf[n]["inputs"]["width"] = a.tam
        wf[n]["inputs"]["height"] = a.tam
    wf["30"]["inputs"]["x"] = a.x
    wf["30"]["inputs"]["y"] = a.y
    if "51" in wf:                      # variante enmascarada
        wf["51"]["inputs"]["x"] = a.x
        wf["51"]["inputs"]["y"] = a.y
    if "80" in wf:                      # variantes hd/hd2/hd3: recorte de detalle
        cx = max(0, a.x - MARGEN)
        cy = max(0, a.y - MARGEN)
        cw = min(LIENZO - cx, a.tam + 2 * MARGEN)
        ch = min(LIENZO - cy, a.tam + 2 * MARGEN)
        for n in ("80", "81"):
            wf[n]["inputs"].update(x=cx, y=cy, width=cw, height=ch)
        wf["91"]["inputs"].update(x=cx, y=cy)
        wf["90"]["inputs"].update(width=cw, height=ch)
        if "93" in wf:                  # hd2/hd3: aspecto y prompt propios
            dw, dh = escalado(cw, ch, DETALLE_RES2)
            for n in ("82", "84"):
                wf[n]["inputs"].update(width=dw, height=dh)
            if a.prompt_detalle:
                wf["93"]["inputs"]["text"] = a.prompt_detalle
        else:                           # hd: escalado cuadrado, como estaba
            for n in ("82", "84"):
                wf[n]["inputs"].update(width=1024, height=1024)
    return wf


async def lanzar(wf: dict, timeout: float) -> dict:
    cli = Serverless(api_key=api_key())
    try:
        ep = await cli.get_endpoint(name=ENDPOINT)
        return await ep.request(
            "/generate/sync",
            {"input": {"request_id": str(uuid.uuid4()), "workflow_json": wf}},
            cost=100, timeout=timeout)
    finally:
        await cli.close()


def salidas(res) -> list[dict]:
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
        return None
    return find(res) or []


async def una(a, denoise: float, dst: Path) -> None:
    a.denoise = denoise
    wf = construir(a)
    print(f"[denoise {denoise}] enviando...", flush=True)
    res = await lanzar(wf, a.timeout)
    outs = salidas(res)
    if not outs:
        print(f"[denoise {denoise}] SIN SALIDAS: {json.dumps(res)[:300]}")
        return
    dst.mkdir(parents=True, exist_ok=True)
    for o in outs:
        nombre = o.get("filename", "?")
        etiqueta = nombre.split("_")[0]          # a / b / c
        ruta = dst / f"{etiqueta}_d{denoise:.2f}.png"
        urllib.request.urlretrieve(o["url"], ruta)
        print(f"[denoise {denoise}] {ruta.name}")
    print(f"[denoise {denoise}] OK ({len(outs)} imagenes)", flush=True)


async def main() -> int:
    p = argparse.ArgumentParser()
    anadir_flags(p)
    p.add_argument("--escena")
    p.add_argument("--personaje")
    p.add_argument("--fusion")
    p.add_argument("--prompt-detalle", help="prompt de la 2a pasada (nodo 93)")
    p.add_argument("--seed-escena", type=int, default=111111)
    p.add_argument("--seed-personaje", type=int, default=222222)
    p.add_argument("--seed-fusion", type=int, default=333333)
    p.add_argument("--denoise", type=float, default=0.4)
    p.add_argument("--sweep", help="lista de denoise separados por coma")
    p.add_argument("--tam", type=int, default=640, help="tamano del personaje en px")
    p.add_argument("--x", type=int, default=200)
    p.add_argument("--y", type=int, default=380)
    p.add_argument("--out", default="escena_out")
    p.add_argument("--timeout", type=float, default=900.0)
    a = p.parse_args()

    dst = HERE / a.out
    valores = [float(v) for v in a.sweep.split(",")] if a.sweep else [a.denoise]
    for i, d in enumerate(valores):
        await una(a, d, dst)
        if i + 1 < len(valores):
            await asyncio.sleep(2)      # margen para el limite de ~1 req/s
    print("FIN")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
