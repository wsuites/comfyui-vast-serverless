#!/usr/bin/env python3
"""Validates the graphs against the real worker schema (nodos.json).

Exists because of a failure that cost a whole sweep: UltimateSDUpscale was
missing `batch_size` (required). ComfyUI does not abort the prompt: it
discards THAT output node ("Output will be ignored") and returns the rest
with status=completed. The serverless runner does not propagate the warning,
so the A/B came out "OK" with the --upscale lever not applied and +1s of
cost.

    python scripts/validar_wf.py                # all combinations
    python scripts/sondear_nodos.py --all -o nodos.json   # refresh schema
"""
from __future__ import annotations
import itertools, json, sys
from pathlib import Path

import construir_wf

HERE = Path(__file__).parent
SCHEMA = HERE / "nodos.json"


def combinations():
    axes = dict(detail=("no", "hd", "hd2"), face=(False, True),
                hands=(None, "yolo", "mesh", "both"), bbox=(False, True),
                vertical=(False, True), upscale=(False, True),
                flip=(False, True))
    for vals in itertools.product(*axes.values()):
        yield dict(zip(axes, vals))


def main() -> int:
    if not SCHEMA.is_file():
        sys.exit(f"missing {SCHEMA}: run `python scripts/sondear_nodos.py --all -o nodos.json`")
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

    failures: list[str] = []
    seen: set[tuple] = set()
    ok = 0
    for kw in combinations():
        try:
            wf = construir_wf.build(**kw)
        except SystemExit:
            continue          # combination forbidden on purpose
        ok += 1
        for nid, node in wf.items():
            ct = node["class_type"]
            spec = schema.get(ct)
            if spec is None:
                failures.append(f"{ct} ({nid}): does not exist on the worker")
                continue
            req = spec["input"].get("required", {})
            given = node.get("inputs", {})
            for field, tipo in req.items():
                if field in given:
                    continue
                # NOTE: having "default" in the schema does NOT save. The
                # default is for the UI; through the API a missing required
                # discards the node even if the schema carries a default
                # (that is how UltimateSDUpscale was lost).
                key = (ct, nid, field)
                if key in seen:
                    continue
                seen.add(key)
                failures.append(f"{ct} ({nid}): missing required `{field}` -> ComfyUI IGNORES its output")
            for field in given:
                if field not in req and field not in spec["input"].get("optional", {}):
                    key = (ct, nid, field, "extra")
                    if key in seen:
                        continue
                    seen.add(key)
                    failures.append(f"{ct} ({nid}): unknown input `{field}`")

    print(f"{ok} combinations built")
    for f in sorted(set(failures)):
        print("  BROKEN:", f)
    print("OK" if not failures else f"{len(set(failures))} problems")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
