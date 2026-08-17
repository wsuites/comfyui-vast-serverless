#!/usr/bin/env python3
"""
Normalizes a strip of RGBA sprites so they work as an animation cycle.

ControlNet imposes the pose but not the SIZE: the model draws the character
bigger or smaller as it pleases, and in animation that shows as a "hiccup".
Here it is fixed in post, which is deterministic and does not depend on the
model obeying.

It does not normalize to a single height: that would enlarge the crouched
poses, which legitimately take less. It normalizes against the height of the
SKELETON that generated each frame, so anatomy is respected and only the
drift is removed.

    python scripts/normalizar.py --in sprites_pose2 --out sprites_norm
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image

from config import ROOT


def alpha_bbox(im: Image.Image):
    a = im.getchannel("A")
    return a.getbbox()          # (left, top, right, bottom) of the non-transparent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="input", default="output/sprites")
    ap.add_argument("--out", dest="output", default="output/sprites/normalized")
    ap.add_argument("--poses", default="output/poses")
    ap.add_argument("--canvas", type=int, default=1024)
    ap.add_argument("--base-height", type=int, default=780,
                    help="height in px of the tallest frame of the cycle")
    ap.add_argument("--ground", type=float, default=0.94)
    ap.add_argument("--flip", default="",
                    help="indices (0-based, comma separated) to flip "
                         "horizontally: the frames that came out facing wrong")
    ap.add_argument("--ms", type=int, default=110,
                    help="ms per frame of the gif (110 = 9fps, looks rushed; "
                         "180-220 is a readable jog)")
    args = ap.parse_args()

    flip = {int(v) for v in args.flip.split(",") if v.strip()}

    src = ROOT / args.input
    dst = ROOT / args.output
    dst.mkdir(parents=True, exist_ok=True)
    L = args.canvas
    ground_y = int(L * args.ground)

    paths = sorted(src.glob("sprite_*.png"))
    if not paths:
        raise SystemExit(f"no sprites in {src}")

    # height and lift of each skeleton: it is the proportion reference
    ref_heights, ref_air = [], []
    for i in range(len(paths)):
        p = ROOT / args.poses / f"pose_{i+1:02d}.png"
        if not p.is_file():
            ref_heights.append(1.0); ref_air.append(0); continue
        sk = Image.open(p).convert("L")
        c = sk.point(lambda v: 255 if v > 20 else 0).getbbox()
        ref_heights.append(c[3] - c[1])
        ref_air.append(int(L * 0.90) - c[3])      # how much it lifts off the ground
    top = max(ref_heights)

    outputs = []
    for i, r in enumerate(paths):
        im = Image.open(r).convert("RGBA")
        if i in flip:
            # the model ignores "running to the right" in some poses; the
            # mirror is the cheap fix and does not touch anatomy
            im = im.transpose(Image.FLIP_LEFT_RIGHT)
        c = alpha_bbox(im)
        if not c:
            print(f"{r.name}: empty, skipped"); continue
        crop = im.crop(c)
        target = args.base_height * (ref_heights[i] / top)
        scale = target / crop.size[1]
        new = (max(1, int(crop.size[0] * scale)),
               max(1, int(crop.size[1] * scale)))
        crop = crop.resize(new, Image.LANCZOS)

        canvas = Image.new("RGBA", (L, L), (0, 0, 0, 0))
        x = (L - new[0]) // 2
        air = int(ref_air[i] * scale)
        y = ground_y - air - new[1]
        canvas.paste(crop, (x, y), crop)
        dest = dst / r.name
        canvas.save(dest)
        outputs.append(canvas)
        print(f"{r.name}: box {c[2]-c[0]}x{c[3]-c[1]} -> {new[0]}x{new[1]}"
              f"  air={air}px")

    if not outputs:
        return
    # sheet on a checkerboard
    T, cols = 256, 4
    rows = (len(outputs) + cols - 1) // cols
    background = Image.new("RGB", (T*cols, T*rows), (235, 235, 235))
    for yy in range(0, T*rows, 16):
        for xx in range(0, T*cols, 16):
            if (xx//16 + yy//16) % 2:
                background.paste((202, 202, 202), (xx, yy, xx+16, yy+16))
    for k, im in enumerate(outputs):
        t = im.resize((T, T), Image.LANCZOS)
        background.paste(t, ((k % cols)*T, (k//cols)*T), t)
    background.save(dst / "normalized_sheet.png")
    print("normalized_sheet.png")

    outputs[0].save(dst / "normalized.gif", save_all=True,
                    append_images=outputs[1:], duration=args.ms, loop=0,
                    disposal=2, transparency=0)
    print("normalized.gif")

    alturas = [alpha_bbox(im)[3] - alpha_bbox(im)[1] for im in outputs]
    pies = [alpha_bbox(im)[3] for im in outputs]
    print("heights:", alturas)
    print("feet line:", pies)


if __name__ == "__main__":
    main()
