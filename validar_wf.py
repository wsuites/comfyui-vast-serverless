#!/usr/bin/env python3
"""Valida los grafos contra el esquema real del worker (nodos.json).

Existe por un fallo que costo un barrido entero: a UltimateSDUpscale le faltaba
`batch_size` (required). ComfyUI no aborta el prompt: descarta ESE nodo de
salida ("Output will be ignored") y devuelve el resto con status=completed.
El runner serverless no propaga el aviso, asi que el A/B salio "OK" con la
palanca --upscale sin aplicar y +1s de coste.

    python validar_wf.py                # todas las combinaciones
    python sondear_nodos.py --todos -o nodos.json   # refrescar el esquema
"""
from __future__ import annotations
import itertools, json, sys
from pathlib import Path

import construir_wf

HERE = Path(__file__).parent
ESQUEMA = HERE / "nodos.json"


def combinaciones():
    ejes = dict(detalle=("no", "hd", "hd2"), cara=(False, True),
                manos=(None, "yolo", "mesh", "ambas"), bbox=(False, True),
                vertical=(False, True), upscale=(False, True))
    for vals in itertools.product(*ejes.values()):
        yield dict(zip(ejes, vals))


def main() -> int:
    if not ESQUEMA.is_file():
        sys.exit(f"falta {ESQUEMA}: corre `python sondear_nodos.py --todos -o nodos.json`")
    esquema = json.loads(ESQUEMA.read_text(encoding="utf-8"))

    fallos: list[str] = []
    vistos: set[tuple] = set()
    ok = 0
    for kw in combinaciones():
        try:
            wf = construir_wf.construir(**kw)
        except SystemExit:
            continue          # combinacion prohibida a proposito
        ok += 1
        for nid, nodo in wf.items():
            ct = nodo["class_type"]
            spec = esquema.get(ct)
            if spec is None:
                fallos.append(f"{ct} ({nid}): no existe en el worker")
                continue
            req = spec["input"].get("required", {})
            dados = nodo.get("inputs", {})
            for campo, tipo in req.items():
                if campo in dados:
                    continue
                # OJO: tener "default" en el esquema NO salva. El default es de
                # la UI; por la API un required ausente descarta el nodo aunque
                # el esquema traiga default (asi se perdio UltimateSDUpscale).
                clave = (ct, nid, campo)
                if clave in vistos:
                    continue
                vistos.add(clave)
                fallos.append(f"{ct} ({nid}): falta required `{campo}` -> ComfyUI IGNORA su salida")
            for campo in dados:
                if campo not in req and campo not in spec["input"].get("optional", {}):
                    clave = (ct, nid, campo, "extra")
                    if clave in vistos:
                        continue
                    vistos.add(clave)
                    fallos.append(f"{ct} ({nid}): input `{campo}` desconocido")

    print(f"{ok} combinaciones construidas")
    for f in sorted(set(fallos)):
        print("  ROTO:", f)
    print("OK" if not fallos else f"{len(set(fallos))} problemas")
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
