"""Configuracion compartida por todo el tooling, leida de .env o del entorno.

Todo lo especifico de un despliegue (identificadores de Vast, bucket, URL de R2)
vive en .env, que NO se versiona. Asi el repo es reutilizable por cualquiera sin
filtrar los identificadores de nadie.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).parent
_ENV = HERE / ".env"
_EJEMPLO = HERE / ".env.example"


def _leer_dotenv() -> dict[str, str]:
    datos: dict[str, str] = {}
    if _ENV.is_file():
        for linea in _ENV.read_text(encoding="utf-8").splitlines():
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            k, v = linea.split("=", 1)
            datos[k.strip()] = v.strip().strip('"').strip("'")
    # el entorno tiene prioridad sobre el fichero
    datos.update({k: v for k, v in os.environ.items() if k in datos or
                  k.startswith(("VAST_", "S3_", "R2_"))})
    return datos


ENV = _leer_dotenv()


def var(nombre: str, defecto: str | None = None) -> str:
    v = ENV.get(nombre, defecto)
    if v is None or v == "":
        sys.exit(f"Falta {nombre}. Copia {_EJEMPLO.name} a .env y rellenalo.")
    return v


def entero(nombre: str, defecto: int | None = None) -> int:
    bruto = ENV.get(nombre)
    if bruto in (None, ""):
        if defecto is not None:
            return defecto
        sys.exit(f"Falta {nombre}. Copia {_EJEMPLO.name} a .env y rellenalo.")
    try:
        return int(bruto)
    except ValueError:
        sys.exit(f"{nombre} debe ser un numero, no {bruto!r}")


# nombre del endpoint: lo usan todos los clientes
ENDPOINT_NAME = ENV.get("VAST_ENDPOINT_NAME", "")


def api_key() -> str:
    """VAST_API_KEY del entorno/.env o el fichero que usa la CLI de vastai.

    Ojo: el SDK evalua os.environ.get("VAST_API_KEY") como valor por defecto de
    un parametro, o sea en tiempo de import. Definir la variable despues no
    sirve; hay que pasarla a Serverless(api_key=...).
    """
    k = (os.environ.get("VAST_API_KEY") or ENV.get("VAST_API_KEY") or "").strip()
    if k:
        return k
    for c in (Path.home()/".config"/"vastai"/"vast_api_key",
              Path.home()/".vast_api_key"):
        if c.is_file() and c.read_text().strip():
            return c.read_text().strip()
    sys.exit("No encuentro la API key: define VAST_API_KEY o usa "
             "'vastai set api-key <KEY>'")


def credenciales_r2() -> dict[str, str]:
    """Credenciales de R2. Solo las necesita el despliegue del provisioning."""
    faltan = [k for k in ("S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY",
                          "S3_BUCKET_NAME", "S3_ENDPOINT_URL") if not ENV.get(k)]
    if faltan:
        sys.exit(f"Faltan credenciales R2: {faltan}. Ponlas en .env o en el entorno.")
    return {
        "S3_ACCESS_KEY_ID": ENV["S3_ACCESS_KEY_ID"],
        "S3_SECRET_ACCESS_KEY": ENV["S3_SECRET_ACCESS_KEY"],
        "S3_BUCKET_NAME": ENV["S3_BUCKET_NAME"],
        "S3_ENDPOINT_URL": ENV["S3_ENDPOINT_URL"],
        "S3_REGION": ENV.get("S3_REGION", "auto"),
    }
