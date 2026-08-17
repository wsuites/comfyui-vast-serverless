"""Shared configuration for all tooling, read from .env or the environment.

Everything deployment-specific (Vast identifiers, bucket, R2 URL) lives in
.env, which is NOT versioned. This keeps the repo reusable by anyone without
leaking identifiers.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# scripts/ directory; the repo root is one level up
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

_ENV = ROOT / ".env"
_EXAMPLE = ROOT / ".env.example"


def _read_dotenv() -> dict[str, str]:
    data: dict[str, str] = {}
    if _ENV.is_file():
        for line in _ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip().strip('"').strip("'")
    # the environment takes priority over the file
    data.update({k: v for k, v in os.environ.items() if k in data or
                 k.startswith(("VAST_", "S3_", "R2_"))})
    return data


ENV = _read_dotenv()


def var(name: str, default: str | None = None) -> str:
    v = ENV.get(name, default)
    if v is None or v == "":
        sys.exit(f"Missing {name}. Copy {_EXAMPLE.name} to .env and fill it in.")
    return v


def entero(name: str, default: int | None = None) -> int:
    raw = ENV.get(name)
    if raw in (None, ""):
        if default is not None:
            return default
        sys.exit(f"Missing {name}. Copy {_EXAMPLE.name} to .env and fill it in.")
    try:
        return int(raw)
    except ValueError:
        sys.exit(f"{name} must be a number, got {raw!r}")


# endpoint name: used by every client
ENDPOINT_NAME = ENV.get("VAST_ENDPOINT_NAME", "")


def api_key() -> str:
    """VAST_API_KEY from env/.env or the file used by the vastai CLI.

    Gotcha: the SDK evaluates os.environ.get("VAST_API_KEY") as the default
    value of a parameter, i.e. at import time. Setting the variable afterwards
    does not work; it must be passed to Serverless(api_key=...).
    """
    k = (os.environ.get("VAST_API_KEY") or ENV.get("VAST_API_KEY") or "").strip()
    if k:
        return k
    for c in (Path.home()/".config"/"vastai"/"vast_api_key",
              Path.home()/".vast_api_key"):
        if c.is_file() and c.read_text().strip():
            return c.read_text().strip()
    sys.exit("API key not found: set VAST_API_KEY or run "
             "'vastai set api-key <KEY>'")

def r2_credentials() -> dict[str, str]:
    """R2 credentials. Only the provisioning deployment needs them."""
    missing = [k for k in ("S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY",
                           "S3_BUCKET_NAME", "S3_ENDPOINT_URL") if not ENV.get(k)]
    if missing:
        sys.exit(f"Missing R2 credentials: {missing}. Set them in .env or the environment.")
    return {
        "S3_ACCESS_KEY_ID": ENV["S3_ACCESS_KEY_ID"],
        "S3_SECRET_ACCESS_KEY": ENV["S3_SECRET_ACCESS_KEY"],
        "S3_BUCKET_NAME": ENV["S3_BUCKET_NAME"],
        "S3_ENDPOINT_URL": ENV["S3_ENDPOINT_URL"],
        "S3_REGION": ENV.get("S3_REGION", "auto"),
    }


def registrar_run(dst: Path, args, extra: dict | None = None) -> Path:
    """Record what produced an output folder: command line + key parameters.

    Writes <dst>/run_info.json so every output set carries its own provenance
    (config, prompts, seeds). Returns the dst Path after creating it.
    """
    dst.mkdir(parents=True, exist_ok=True)
    info: dict = {
        "command": " ".join(sys.argv),
        "argv": sys.argv[1:],
        "cwd": str(Path.cwd()),
    }
    if extra:
        info["extra"] = extra
    (dst / "run_info.json").write_text(
        json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
    return dst
