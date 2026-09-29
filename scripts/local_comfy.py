"""
The local backend: a ComfyUI running on this machine instead of a rented one.

Both the web console and ``call_endpoint.py`` can render against it. It is the
same ``vastai/comfy`` image provisioned by the same ``serverless_provision.sh``,
run in Docker on WSL2 with the GPU passed through, so the workflows are
byte-for-byte the ones Vast gets: nothing here builds a graph of its own.

What it skips is everything that exists because the GPU is rented: no offer
search, no boot wait, no lease, no idle release, no cost. It only talks to
ComfyUI's own HTTP API, through the helpers ``fleet`` already uses for rented
workers.

Configuration (``.env`` or the environment):
    COMFY_BACKEND    vast | local   - default for the web console and the CLI
    LOCAL_COMFY_URL  where the container answers (default http://127.0.0.1:18188)
"""
from __future__ import annotations

import os
import urllib.error
import urllib.parse

import config
import fleet

BACKENDS = ("vast", "local")


def _setting(name: str, default: str) -> str:
    return os.environ.get(name) or config.ENV.get(name) or default


LOCAL_URL = _setting("LOCAL_COMFY_URL", "http://127.0.0.1:18188").rstrip("/")
# Set when LOCAL_COMFY_URL is the tunnel to scripts/local_gate.py rather than
# the container itself (the VPS case). Unset on the machine that runs Docker.
LOCAL_TOKEN = _setting("LOCAL_COMFY_TOKEN", "")
if LOCAL_TOKEN and not LOCAL_URL.startswith(("http://127.", "http://localhost")):
    fleet.AUTH_HEADERS[LOCAL_URL] = {"Authorization": f"Bearer {LOCAL_TOKEN}"}


def default_backend() -> str:
    value = _setting("COMFY_BACKEND", "vast").strip().lower()
    if value not in BACKENDS:
        raise SystemExit(f"COMFY_BACKEND={value!r}: expected one of {BACKENDS}")
    return value


def probe(url: str = LOCAL_URL) -> dict:
    """GPU name, VRAM and ComfyUI version, or ``{"ready": False}``.

    ``/system_stats`` rather than ``/object_info``: it is a few hundred bytes
    against several megabytes, and the header polls it every ten seconds.
    """
    try:
        stats = fleet.http(f"{url}/system_stats", timeout=3)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ready": False, "url": url, "detail": str(exc)}
    dev = ((stats or {}).get("devices") or [{}])[0]
    system = (stats or {}).get("system") or {}
    gib = 1024 ** 3
    total = dev.get("vram_total")
    free = dev.get("vram_free")
    return {
        "ready": True,
        "url": url,
        # "cuda:0 NVIDIA GeForce RTX 5070 : cudaMallocAsync" -> the card name
        "gpu": str(dev.get("name") or "local GPU").split(" : ")[0]
                                                  .removeprefix("cuda:0 "),
        "vram_total": total / gib if total else None,
        "vram": (total - free) / gib if total and free is not None else None,
        "comfyui": system.get("comfyui_version"),
    }


def view_urls(url: str, entry: dict) -> list[dict]:
    """``/view`` links for every image a finished prompt saved.

    The CLI's contract with ``call_endpoint.py`` is a response carrying image
    URLs, which it then downloads. Vast hands back presigned R2 links; here the
    bytes are already on a server we can reach, so the links point at it.
    """
    out = []
    for node in (entry.get("outputs") or {}).values():
        for img in node.get("images") or []:
            if img.get("type") not in (None, "output"):
                continue
            q = urllib.parse.urlencode({
                "filename": img.get("filename", ""),
                "subfolder": img.get("subfolder", ""),
                "type": img.get("type", "output"),
            })
            out.append({"url": f"{url}/view?{q}",
                        "filename": img.get("filename", "")})
    return out


def render(workflow: dict, timeout: float, url: str = LOCAL_URL) -> dict:
    """Submit, wait, and answer in the shape the serverless wrapper uses."""
    state = probe(url)
    if not state["ready"]:
        raise RuntimeError(
            f"Local ComfyUI is not answering at {url} ({state.get('detail')}). "
            "Start it with: wsl -d Debian -u root -- docker start comfy-local")
    missing = fleet.missing_inputs(url, workflow)
    if missing:
        raise RuntimeError("Local ComfyUI is missing: " + ", ".join(missing))
    pid = fleet.submit(url, workflow, "cli-local")
    entry = fleet.wait_job(url, pid, timeout, hold_lease=False)
    return {
        "status": "success",
        "backend": "local",
        "prompt_id": pid,
        "images": view_urls(url, entry),
    }
