#!/usr/bin/env python3
"""Vuelca el esquema real de nodos de ComfyUI desde el worker vivo.

ComfyUI (18188) no esta publicado fuera: solo el 22 (SSH) y el 3000 (api
wrapper). Asi que esto entra por SSH a la instancia que este corriendo y hace
curl a 127.0.0.1:18188/object_info. Sin esto no se puede cablear un nodo nuevo:
los nombres de entrada/salida hay que verlos, no adivinarlos.

    python sondear_nodos.py                      # lista los nodos de recorte
    python sondear_nodos.py --buscar crop bbox   # filtra por subcadena
    python sondear_nodos.py --nodo CropByBBoxes  # esquema completo de uno
    python sondear_nodos.py --todos -o nodos.json
"""
from __future__ import annotations

import argparse, json, os, subprocess, sys, urllib.request
from pathlib import Path

from config import api_key

CLAVE = Path.home() / ".ssh" / "xcl"
# lo que interesa para recortar el personaje a su bounding box
BUSQUEDA = ("crop", "bbox", "bounding", "mask", "trim")


def instancia() -> tuple[str, int]:
    r = urllib.request.Request("https://console.vast.ai/api/v0/instances/",
                               headers={"Authorization": "Bearer " + api_key()})
    ins = json.load(urllib.request.urlopen(r, timeout=30)).get("instances", [])
    vivas = [i for i in ins if i.get("actual_status") == "running"
             and i.get("public_ipaddr") and i.get("ssh_port")]
    if not vivas:
        sys.exit("No hay ninguna instancia viva. Lanza una peticion al endpoint "
                 "para que el autoscaler arranque un worker y reintenta.")
    i = vivas[0]
    # OJO: 'ssh_port' es el del proxy (ssh2.vast.ai), no el de la IP publica.
    # El puerto directo esta en el mapeo de puertos del contenedor.
    mapeo = (i.get("ports") or {}).get("22/tcp") or []
    puerto = int(mapeo[0]["HostPort"]) if mapeo else int(i["ssh_port"])
    return i["public_ipaddr"].strip(), puerto


def object_info(host: str, puerto: int) -> dict:
    cmd = ["ssh", "-i", str(CLAVE), "-o", "IdentitiesOnly=yes",
           "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15",
           "-p", str(puerto), f"root@{host}",
           "curl -s --max-time 60 http://127.0.0.1:18188/object_info"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if p.returncode != 0 or not p.stdout.strip():
        sys.exit(f"ssh/curl fallo ({p.returncode}): {p.stderr[:400]}")
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        sys.exit(f"respuesta no JSON: {p.stdout[:400]}")


def resumen(nombre: str, d: dict) -> str:
    req = d.get("input", {}).get("required", {})
    opt = d.get("input", {}).get("optional", {})

    def tipo(v):
        t = v[0] if isinstance(v, list) and v else v
        return t if isinstance(t, str) else "LISTA"

    ent = [f"{k}:{tipo(v)}" for k, v in req.items()]
    ent += [f"[{k}:{tipo(v)}]" for k, v in opt.items()]
    sal = list(zip(d.get("output", []), d.get("output_name", []) or d.get("output", [])))
    salida = ", ".join(f"{n}({t})" for t, n in sal)
    return (f"{nombre}\n    in : {', '.join(ent) or '-'}\n"
            f"    out: {salida or '-'}\n    pack: {d.get('python_module', '?')}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--buscar", nargs="*", default=list(BUSQUEDA))
    p.add_argument("--nodo", action="append", default=[],
                   help="esquema crudo de un nodo concreto (repetible)")
    p.add_argument("--todos", action="store_true")
    p.add_argument("-o", "--salida", help="guarda el object_info entero")
    a = p.parse_args()

    host, puerto = instancia()
    print(f"# worker {host}:{puerto}", flush=True)
    info = object_info(host, puerto)
    print(f"# {len(info)} nodos registrados\n")

    if a.salida:
        Path(a.salida).write_text(json.dumps(info, indent=2), encoding="utf-8")
        print(f"volcado -> {a.salida}\n")

    if a.nodo:
        for n in a.nodo:
            if n not in info:
                print(f"{n}: NO EXISTE en este worker")
                continue
            print(json.dumps({n: info[n]}, indent=2, ensure_ascii=False))
        return 0

    nombres = sorted(info) if a.todos else \
        sorted(n for n in info if any(s in n.lower() for s in a.buscar))
    for n in nombres:
        print(resumen(n, info[n]))
    print(f"\n({len(nombres)} nodos)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
