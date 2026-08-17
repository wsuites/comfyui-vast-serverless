from PIL import Image, ImageDraw
from pathlib import Path
import sys

from config import ROOT

# 6 good ones from the old batch + 6 redone with upscale
COMBOS = ["base","v","l","vl","b","vb","u","vu","lu","vlu","vbu","vblu"]
DIRS = [ROOT / "output/ab-resolution/ab_up4",
        ROOT / "output/ab-resolution/ab_res12"]   # up4 wins (redone)
def find(c, pre):
    for d in DIRS:
        p = d / f"{pre}_{c}.png"
        if p.is_file(): return p
    return None
def sheet(pre, dst, H=620, cols=6):
    ims, labs = [], []
    for c in COMBOS:
        p = find(c, pre)
        if not p: continue
        im = Image.open(p).convert("RGB")
        w = max(1, round(im.width * H / im.height))
        ims.append(im.resize((w, H), Image.LANCZOS))
        labs.append(f"{c}  {Image.open(p).size[0]}x{Image.open(p).size[1]}")
    if not ims: return print(f"no {pre}")
    rows = [(ims[i:i+cols], labs[i:i+cols]) for i in range(0, len(ims), cols)]
    W = max(sum(im.width for im in r[0]) for r in rows)
    out = Image.new("RGB", (W, (H+26)*len(rows)), (250,250,250))
    d = ImageDraw.Draw(out)
    y = 0
    for ims_r, labs_r in rows:
        x = 0
        for im, lab in zip(ims_r, labs_r):
            out.paste(im, (x, y+22)); d.text((x+6, 6+y), lab, fill=(0,0,0)); x += im.width
        y += H+26
    out.save(dst); print(f"SHEET {dst} ({len(ims)} combos) {out.size}")

out_dir = ROOT / "output" / "ab-resolution"
sheet("f", out_dir / "HOJA_12_final.png")
sheet("c", out_dir / "HOJA_12_fused.png")
