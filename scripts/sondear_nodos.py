#!/usr/bin/env python3
"""Dumps the real ComfyUI node schema from the live worker.

ComfyUI (18188) is not published outside: only port 22 (SSH) and 3000 (api
wrapper). So this goes in via SSH to the running instance and curls
127.0.0.1:18188/object_info. Without this a new node cannot be wired: input/
output names must be seen, not guessed.

    python scripts/sondear_nodos.py                      # lists crop nodes
    python scripts/sondear_nodos.py --search crop bbox   # filter by substring
    python scripts/sondear_nodos.py --node CropByBBoxes  # full schema of one
    python scripts/sondear_nodos.py --all -o nodos.json
"""
from __future__ import annotations

import argparse, json, os, subprocess, sys, urllib.request
from pathlib import Path

from config import api_key

KEY = Path.home() / ".ssh" / "xcl"
# what matters for cropping the character to its bounding box
SEARCH = ("crop", "bbox", "bounding", "mask", "trim")


def instance() -> tuple[str, int]:
    r = urllib.request.Request("https://console.vast.ai/api/v0/instances/",
                               headers={"Authorization": "Bearer " + api_key()})
    ins = json.load(urllib.request.urlopen(r, timeout=30)).get("instances", [])
    alive = [i for i in ins if i.get("actual_status") == "running"
             and i.get("public_ipaddr") and i.get("ssh_port")]
    if not alive:
        sys.exit("No live instance. Send a request to the endpoint so the "
                 "autoscaler starts a worker and retry.")
    i = alive[0]
    # NOTE: 'ssh_port' is the proxy port (ssh2.vast.ai), not the public IP one.
    # The direct port is in the container port mapping.
    mapping = (i.get("ports") or {}).get("22/tcp") or []
    port = int(mapping[0]["HostPort"]) if mapping else int(i["ssh_port"])
    return i["public_ipaddr"].strip(), port


def object_info(host: str, port: int) -> dict:
    cmd = ["ssh", "-i", str(KEY), "-o", "IdentitiesOnly=yes",
           "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15",
           "-p", str(port), f"root@{host}",
           "curl -s --max-time 60 http://127.0.0.1:18188/object_info"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if p.returncode != 0 or not p.stdout.strip():
        sys.exit(f"ssh/curl failed ({p.returncode}): {p.stderr[:400]}")
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        sys.exit(f"non-JSON response: {p.stdout[:400]}")


def summary(name: str, d: dict) -> str:
    req = d.get("input", {}).get("required", {})
    opt = d.get("input", {}).get("optional", {})

    def type_of(v):
        t = v[0] if isinstance(v, list) and v else v
        return t if isinstance(t, str) else "LIST"

    ins = [f"{k}:{type_of(v)}" for k, v in req.items()]
    ins += [f"[{k}:{type_of(v)}]" for k, v in opt.items()]
    outs = list(zip(d.get("output", []), d.get("output_name", []) or d.get("output", [])))
    out_str = ", ".join(f"{n}({t})" for t, n in outs)
    return (f"{name}\n    in : {', '.join(ins) or '-'}\n"
            f"    out: {out_str or '-'}\n    pack: {d.get('python_module', '?')}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--search", nargs="*", default=list(SEARCH))
    p.add_argument("--node", action="append", default=[],
                   help="raw schema of one node (repeatable)")
    p.add_argument("--all", action="store_true")
    p.add_argument("-o", "--output", help="save the whole object_info")
    a = p.parse_args()

    host, port = instance()
    print(f"# worker {host}:{port}", flush=True)
    info = object_info(host, port)
    print(f"# {len(info)} registered nodes\n")

    if a.output:
        Path(a.output).write_text(json.dumps(info, indent=2), encoding="utf-8")
        print(f"dump -> {a.output}\n")

    if a.node:
        for n in a.node:
            if n not in info:
                print(f"{n}: DOES NOT EXIST on this worker")
                continue
            print(json.dumps({n: info[n]}, indent=2, ensure_ascii=False))
        return 0

    names = sorted(info) if a.all else \
        sorted(n for n in info if any(s in n.lower() for s in a.search))
    for n in names:
        print(summary(n, info[n]))
    print(f"\n({len(names)} nodes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
