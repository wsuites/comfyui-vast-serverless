#!/usr/bin/env python3
"""
Generates OpenPose skeletons (COCO-18) of a running cycle, side view facing
right.

We do not depend on the detector: we draw the skeletons ourselves, which was
the failing piece (OpenposePreprocessor does not read anime style well).

    python scripts/ciclo_pose.py --frames 8 --out poses
"""
from __future__ import annotations

import argparse, math
from pathlib import Path

from PIL import Image, ImageDraw

from config import ROOT

# COCO-18: 0 nose 1 neck 2 rShoulder 3 rElbow 4 rWrist 5 lShoulder 6 lElbow
# 7 lWrist 8 rHip 9 rKnee 10 rAnkle 11 lHip 12 lKnee 13 lAnkle
# 14 rEye 15 lEye 16 rEar 17 lEar
LIMBS = [(1,2),(1,5),(2,3),(3,4),(5,6),(6,7),(1,8),(8,9),(9,10),
         (1,11),(11,12),(12,13),(1,0),(0,14),(14,16),(0,15),(15,17)]
COLORS = [(255,0,0),(255,85,0),(255,170,0),(255,255,0),(170,255,0),(85,255,0),
          (0,255,0),(0,255,85),(0,255,170),(0,255,255),(0,170,255),(0,85,255),
          (0,0,255),(85,0,255),(170,0,255),(255,0,255),(255,0,170),(255,0,85)]

# 4 key poses of the cycle. Angles in degrees from vertical downward,
# positive = forward (right).
#   (thigh, knee_flex)  and  (upper_arm, elbow_flex)
# 'air' = how much the lowest foot separates from the ground. Anchoring to the
# ground is what keeps the character from changing size between frames: the
# limb lengths are already constant, what varied was the height of the whole.
KEYS = [
    # contact: leading leg extended touching the ground
    dict(air=0,  thigh_a=28,  knee_a=12,  thigh_b=-30, knee_b=58,
         arm_a=-38, elb_a=55,  arm_b=38,  elb_b=70),
    # cushioning: weight on the support leg, body low
    dict(air=0,  thigh_a=6,   knee_a=48,  thigh_b=-44, knee_b=95,
         arm_a=-18, elb_a=70,  arm_b=20,  elb_b=80),
    # stride: the back leg moves forward with the knee high
    dict(air=14, thigh_a=-16, knee_a=18,  thigh_b=12,  knee_b=105,
         arm_a=12,  elb_a=75,  arm_b=-14, elb_b=60),
    # push-off: flight phase, both feet off the ground
    dict(air=58, thigh_a=-36, knee_a=32,  thigh_b=42,  knee_b=76,
         arm_a=34,  elb_a=60,  arm_b=-36, elb_b=55),
]

GROUND = 0.90        # ground line, as a fraction of the canvas height


def rad(g): return math.radians(g)


def point(origin, angle_deg, length):
    """From 'origin', a segment of 'length' at an angle from vertical."""
    a = rad(angle_deg)
    return (origin[0] + length * math.sin(a), origin[1] + length * math.cos(a))


def skeleton(key, w, h, mirror=False):
    """Returns the 18 keypoints. mirror=True swaps the two legs/arms."""
    k = dict(key)
    if mirror:
        k = dict(k, thigh_a=key["thigh_b"], knee_a=key["knee_b"],
                 thigh_b=key["thigh_a"], knee_b=key["knee_a"],
                 arm_a=key["arm_b"], elb_a=key["elb_b"],
                 arm_b=key["arm_a"], elb_b=key["elb_a"])

    cx = w * 0.46
    hip_y = h * 0.52
    L_THIGH, L_SHIN = h * 0.165, h * 0.165
    L_TORSO = h * 0.235
    L_ARM, L_FORE = h * 0.125, h * 0.115
    LEAN = 14                                    # torso lean when running

    hip = (cx, hip_y)
    neck = point(hip, 180 + LEAN, L_TORSO)       # upward, leaning
    nose = point(neck, 180 + LEAN - 8, h * 0.085)

    p = [None] * 18
    p[1] = neck
    p[0] = nose
    p[8] = p[11] = hip
    p[2] = p[5] = (neck[0], neck[1] + h * 0.012)

    # legs
    for side, (thigh, knee_f, i_knee, i_ankle) in enumerate(
            [(k["thigh_a"], k["knee_a"], 9, 10), (k["thigh_b"], k["knee_b"], 12, 13)]):
        knee = point(hip, thigh, L_THIGH)
        p[i_knee] = knee
        p[i_ankle] = point(knee, thigh - knee_f, L_SHIN)

    # arms (opposite to the legs)
    for side, (arm, elb_f, i_elb, i_wrist) in enumerate(
            [(k["arm_a"], k["elb_a"], 3, 4), (k["arm_b"], k["elb_b"], 6, 7)]):
        elbow = point(p[2], arm, L_ARM)
        p[i_elb] = elbow
        p[i_wrist] = point(elbow, arm + elb_f, L_FORE)

    # profile face looking right
    d = h * 0.018
    p[14] = (nose[0] - d * 0.6, nose[1] - d)      # right eye
    p[15] = (nose[0] - d * 1.2, nose[1] - d)      # left eye (hidden)
    p[16] = (neck[0] + d * 0.4, neck[1] - h * 0.055)
    p[17] = (neck[0] - d * 0.4, neck[1] - h * 0.055)

    # anchor to the ground: the lowest foot lands on the ground line, except
    # the intentional 'air' lift. This way the character does not change size
    # or float between frames.
    lowest_foot = max(p[10][1], p[13][1])
    dy = (h * GROUND - k["air"]) - lowest_foot
    p = [(q[0], q[1] + dy) for q in p]
    return p


def draw(p, w, h):
    img = Image.new("RGB", (w, h), (0, 0, 0))
    dr = ImageDraw.Draw(img)
    thickness = max(4, w // 128)
    for i, (a, b) in enumerate(LIMBS):
        if p[a] and p[b]:
            dr.line([p[a], p[b]], fill=COLORS[i % len(COLORS)], width=thickness)
    r = max(3, w // 200)
    for i, q in enumerate(p):
        if q:
            dr.ellipse([q[0]-r, q[1]-r, q[0]+r, q[1]+r], fill=COLORS[i % len(COLORS)])
    return img


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=8)
    ap.add_argument("--size", type=int, default=1024)
    ap.add_argument("--out", default="output/poses")
    a = ap.parse_args()

    dst = ROOT / a.out
    dst.mkdir(parents=True, exist_ok=True)
    n, s = a.frames, a.size
    for i in range(n):
        # first half of the cycle with one leg, second with the other
        fase = i * len(KEYS) * 2 // n
        key = KEYS[fase % len(KEYS)]
        mirror = (fase // len(KEYS)) % 2 == 1
        img = draw(skeleton(key, s, s, mirror), s, s)
        ruta = dst / f"pose_{i+1:02d}.png"
        img.save(ruta)
        print(f"{ruta.name}  key={fase % len(KEYS)} mirror={mirror}")

    # contact strip to review the cycle at a glance
    ims = [Image.open(dst / f"pose_{i+1:02d}.png") for i in range(n)]
    T = 200
    tira = Image.new("RGB", (T*n, T), (0, 0, 0))
    for i, im in enumerate(ims):
        tira.paste(im.resize((T, T), Image.LANCZOS), (i*T, 0))
    tira.save(dst / "cycle_strip.png")
    print("cycle_strip.png")


if __name__ == "__main__":
    main()
