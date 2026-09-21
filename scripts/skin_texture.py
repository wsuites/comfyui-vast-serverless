#!/usr/bin/env python3
"""Texture measured on skin only, for comparing different models.

The metric in ``ab_modelo.measure`` is a neighbouring-pixel difference over the
whole frame. That is fine for comparing variants of one model, where the
composition is held fixed and only the surface moves. Across two models with
different drawing styles it also counts line art: harder outlines, separated
hair strands and cheek hatching all register as "texture" while the skin stays
flat. Anima scores its first seed at 7.37 that way and looks like a win.

This scores every patch, keeps the skin-coloured ones and averages the flattest
quartile, which is surface micro-detail with the lineart outliers dropped.

Usage:
    python scripts/skin_texture.py output/ab-model/*/  [--patch 64]
    python scripts/skin_texture.py output/ab-model/anima/anima_101000.png
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

# Skin in HSV, on PIL's 0-255 scale for all three channels. Hue 3-30 is the
# warm band that holds every skin tone these models produce; the saturation
# ceiling keeps saturated red hair out, and the value floor keeps shadow out.
SKIN_HUE = (3, 30)
SKIN_SAT = (25, 120)
SKIN_VAL_MIN = 110


def patch_texture(gray: Image.Image) -> float:
    """Same high-frequency energy as ab_modelo.measure: dx + dy, summed."""
    w, h = gray.size
    dx = ImageChops.difference(gray.crop((1, 0, w, h)), gray.crop((0, 0, w - 1, h)))
    dy = ImageChops.difference(gray.crop((0, 1, w, h)), gray.crop((0, 0, w, h - 1)))
    return ImageStat.Stat(dx).mean[0] + ImageStat.Stat(dy).mean[0]


def profile(path: Path, n: int = 64) -> dict[str, float] | None:
    """Skin-patch texture profile, or None when no skin was found."""
    im = Image.open(path).convert("RGB")
    hsv = im.convert("HSV")
    gray = im.convert("L")
    w, h = im.size

    values: list[float] = []
    for y in range(0, h - n + 1, n // 2):  # half-patch stride, patches overlap
        for x in range(0, w - n + 1, n // 2):
            box = (x, y, x + n, y + n)
            hue, sat, val = ImageStat.Stat(hsv.crop(box)).mean
            if not (SKIN_HUE[0] <= hue <= SKIN_HUE[1]):
                continue
            if not (SKIN_SAT[0] <= sat <= SKIN_SAT[1]):
                continue
            if val < SKIN_VAL_MIN:
                continue
            values.append(patch_texture(gray.crop(box)))

    if not values:
        return None

    values.sort()
    flattest = values[: max(1, len(values) // 4)]
    return {
        "patches": len(values),
        "flat": sum(flattest) / len(flattest),
        "median": statistics.median(values),
        "whole": patch_texture(gray),
    }


def collect(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    return sorted(p for p in target.glob("*.png") if p.name != "ab_model.png")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="+", type=Path,
                    help="image files, or directories of them (one per arm)")
    ap.add_argument("--patch", type=int, default=64,
                    help="patch size in pixels (default 64)")
    ap.add_argument("--per-image", action="store_true",
                    help="also print a line per image")
    args = ap.parse_args()

    # A directory is one arm. Loose files are one arm too -- a shell glob of an
    # arm's images must aggregate, not report each image on its own.
    groups: list[tuple[str, list[Path]]] = []
    loose = [t for t in args.targets if t.is_file()]
    if loose:
        stem = os.path.commonprefix([p.stem for p in loose]) or loose[0].parent.name
        groups.append((f"{loose[0].parent}/{stem}*", loose))
    for target in args.targets:
        if not target.is_file():
            groups.append((str(target), collect(target)))

    for label, files in groups:
        if not files:
            print(f"{label}: no images", file=sys.stderr)
            continue

        rows = []
        for f in files:
            r = profile(f, args.patch)
            if r is None:
                print(f"  {f.name}: no skin patches found", file=sys.stderr)
                continue
            rows.append(r)
            if args.per_image:
                print(f"  {f.name:28s} whole={r['whole']:5.2f} "
                      f"skin_flat={r['flat']:5.2f} patches={r['patches']}")

        if not rows:
            continue

        flat = [r["flat"] for r in rows]
        print(f"{label:40s} n={len(rows):2d} "
              f"whole={sum(r['whole'] for r in rows) / len(rows):5.2f} "
              f"skin_flat={sum(flat) / len(flat):5.2f} "
              f"(sd {statistics.pstdev(flat):4.2f}) "
              f"skin_med={sum(r['median'] for r in rows) / len(rows):5.2f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
