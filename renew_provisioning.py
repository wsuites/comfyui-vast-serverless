#!/usr/bin/env python3
"""
Sube serverless_provision.sh a R2 y deja el template del endpoint apuntando a el.

Correr despues de editar serverless_provision.sh:

    python renew_provisioning.py
    python renew_provisioning.py --update-workers   # ademas refresca workers vivos

PROVISIONING_SCRIPT apunta al Public Development URL del bucket (R2_PUBLIC_BASE),
que es permanente y no caduca. Con --presigned se genera en su lugar una URL
firmada de 7 dias, util si algun dia se desactiva el acceso publico.

Credenciales: variables de entorno S3_* o un fichero .env al lado de este script.
(Vast enmascara los valores en `vastai show env-vars`, no se pueden leer de ahi.)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
SCRIPT = HERE / "serverless_provision.sh"
import config
# Public Development URL del bucket (R2 -> Settings -> Public Development URL).
# Permanente y sin query string, a diferencia de una presigned.
BUCKET_KEY = config.var("R2_PROVISION_KEY")
R2_PUBLIC_BASE = config.var("R2_PUBLIC_BASE")
TEMPLATE_ID = config.entero("VAST_TEMPLATE_ID")
TEMPLATE_NAME = config.var("VAST_TEMPLATE_NAME")
WORKERGROUP_ID = config.entero("VAST_WORKERGROUP_ID")
ENDPOINT_ID = config.entero("VAST_ENDPOINT_ID")
IMAGE = config.var("VAST_IMAGE")
IMAGE_TAG = config.var("VAST_IMAGE_TAG")
HF_TOKEN = config.var("VAST_HF_TOKEN")
EXPIRES = 7 * 24 * 3600  # maximo que admite SigV4

ONSTART = """export SERVERLESS=true
export BACKEND=comfyui-json
export COMFYUI_API_BASE="http://localhost:18188"
export MODEL_LOG=/var/log/portal/comfyui.log;
entrypoint.sh &
wget -O - "https://raw.githubusercontent.com/vast-ai/pyworker/main/start_server.sh" | bash"""

# Disco asignado por worker. Se paga por GB ASIGNADO, no usado, y 24/7.
# Medido con todo instalado (modelos + Impact + UltimateSDUpscale + RMBG):
# la capa de escritura ocupa 9.4GB, asi que en 16GB quedan ~6.6GB de holgura.
# OJO: `du -sx /` da 22GB, pero eso incluye las capas de solo lectura de la
# imagen docker, que NO cuentan para la cuota. El numero bueno es el de `df /`.
# Ademas disk_space>=16 en los filtros abre muchos mas hosts baratos que >=32.
DISK_SPACE = config.var("VAST_DISK_SPACE")

# Filtros de oferta. Claves del coste real:
#   storage_cost  -> $/GB/mes. 0.0625 * 32GB = 2$/mes como techo.
#                    (la mediana del mercado es 0.20, o sea 6.4$/mes a 32GB)
#   inet_down_cost-> $/GB. 0.005 = 5$/TB. Un arranque en frio baja ~22GB
#                    (imagen + modelos), o sea ~3 centimos. Apretar mas este
#                    filtro recorta muchisimo el abanico para ahorrar nada.
#   dlperf        -> suelo de rendimiento, para no acabar en una 4060 Ti.
#   dph_total     -> techo OBLIGATORIO. Sin el, entre las maquinas con disco
#                    barato hay H100 a 4.26$/h y el autoscaler puede cogerlas.
# Sin verified ni reliability: no importan para esta carga.
SEARCH_PARAMS = config.var("VAST_SEARCH_PARAMS")

# NOTA: los workergroups serverless NO soportan instancias interruptibles.
# Probado el 2026-08-15: con `--launch_args "--bid_price 0.15"` la instancia
# creada sale con is_bid=False. Solo on-demand.




def vastai(*args: str) -> str:
    proc = subprocess.run(["vastai", *args], capture_output=True, text=True,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    if proc.returncode != 0:
        sys.exit(f"fallo `vastai {' '.join(args)}`:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--update-workers", action="store_true",
                    help="ademas, fuerza un rolling update de los workers vivos")
    ap.add_argument("--presigned", action="store_true",
                    help="usar una URL firmada de 7 dias en vez de la publica")
    args = ap.parse_args()

    if not SCRIPT.is_file():
        sys.exit(f"no encuentro {SCRIPT}")

    env = config.credenciales_r2()
    import boto3

    # el script tiene que ir con saltos de linea LF o bash se atraganta
    data = SCRIPT.read_bytes().replace(b"\r\n", b"\n")
    SCRIPT.write_bytes(data)

    s3 = boto3.client("s3", endpoint_url=env["S3_ENDPOINT_URL"],
                      aws_access_key_id=env["S3_ACCESS_KEY_ID"],
                      aws_secret_access_key=env["S3_SECRET_ACCESS_KEY"],
                      region_name=env["S3_REGION"])
    bucket = env["S3_BUCKET_NAME"]

    s3.put_object(Bucket=bucket, Key=BUCKET_KEY, Body=data,
                  ContentType="text/x-shellscript")
    print(f"subido a r2://{bucket}/{BUCKET_KEY} ({len(data)} bytes)")

    if args.presigned:
        url = s3.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": BUCKET_KEY}, ExpiresIn=EXPIRES)
        print("URL presignada regenerada (caduca en 7 dias)")
    else:
        url = f"{R2_PUBLIC_BASE}/{BUCKET_KEY}"
        # comprobamos que el acceso publico sigue activo antes de romper el template.
        # Cloudflare devuelve 403 al User-Agent por defecto de urllib, asi que nos
        # hacemos pasar por curl, que es lo que usa el provisioner del worker.
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                served = r.read()
        except Exception as e:
            sys.exit(f"la URL publica no responde ({e}).\n"
                     f"Comprueba el Public Development URL del bucket, o usa --presigned.")
        if served != data:
            sys.exit("la URL publica sirve un contenido distinto al que acabamos de subir "
                     "(cache de R2?). Reintenta en unos segundos o usa --presigned.")
        print(f"URL publica verificada ({len(served)} bytes): {url}")

    template_env = (
        '-p 3000:3000 '
        '-e COMFYUI_ARGS="--disable-auto-launch --port 18188" '
        f'-e HF_TOKEN={HF_TOKEN} '
        '-e BENCHMARK_TEST_WIDTH=512 -e BENCHMARK_TEST_HEIGHT=512 '
        '-e BENCHMARK_TEST_STEPS=20 '
        f'-e PROVISIONING_SCRIPT="{url}"'
    )

    # ojo: update template cambia el hash_id, hay que releerlo despues
    vastai("update", "template", current_hash(),
           "--name", TEMPLATE_NAME,
           "--image", IMAGE, "--image_tag", IMAGE_TAG,
           "--ssh", "--direct", "--disk_space", DISK_SPACE,
           "--env", template_env,
           "--onstart-cmd", ONSTART,
           "--search_params", SEARCH_PARAMS,
           "--no-default")   # sin verified=true forzado
    new_hash = current_hash()
    print(f"template {TEMPLATE_ID} actualizado -> hash {new_hash}")

    # Los search_params hay que repetirlos AQUI ademas de en el template: si
    # solo se ponen en el template, el workergroup vuelve a inyectar
    # verified=true por su cuenta pese al --no-default, y eso deja el pool en
    # 1 sola oferta -> el autoscaler relaja el tope de precio y coge maquinas
    # caras. Con -n aqui, la search_query almacenada queda limpia.
    vastai("update", "workergroup", str(WORKERGROUP_ID),
           "--endpoint_id", str(ENDPOINT_ID),
           "--template_hash", new_hash, "--template_id", str(TEMPLATE_ID),
           "--launch_args", "", "--gpu_ram", "16",
           "--search_params", SEARCH_PARAMS, "-n")
    print(f"workergroup {WORKERGROUP_ID} re-apuntado (search_params forzados)")

    if args.update_workers:
        vastai("update", "workers", str(WORKERGROUP_ID))
        print("rolling update de workers lanzado")

    if args.presigned:
        print("\nlisto. OJO: esta URL caduca en 7 dias.")
    else:
        print("\nlisto. la URL es permanente, no hay que renovar nada.")
    return 0


def current_hash() -> str:
    out = vastai("search", "templates", f"creator_id={config.entero('VAST_CREATOR_ID')}", "--raw")
    data = json.loads(out)
    templates = data.get("templates", data) if isinstance(data, dict) else data
    for t in templates:
        if t.get("id") == TEMPLATE_ID:
            return t["hash_id"]
    sys.exit(f"no encuentro el template {TEMPLATE_ID}")


if __name__ == "__main__":
    sys.exit(main())
