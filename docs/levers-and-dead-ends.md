# Levers: what each one actually does, and what to stop trying

Measured on `waiIllustriousSDXL_v170`, 2026-08-17. Every number here comes from
changing **one** variable with the seed fixed. The dead ends are documented on
purpose: three of them look obvious and are wrong, and they cost real GPU time
to disprove.

---

## Placing a character on a scene: measure, do not eyeball

`b_collage` minus `a_scene` **is** the pasted silhouette — the paste is hard,
with no fusion in between — so it gives the figure's exact bounding box in
canvas coordinates:

```python
X = np.asarray(Image.open(a_scene).convert("RGB").resize((1536, 1536)), float)
Y = np.asarray(Image.open(b_collage).convert("RGB").resize((1536, 1536)), float)
ys, xs = np.where(np.abs(X - Y).sum(2) > 30)
```

Two things fall out of it, and both settled arguments that eyeballing could not.

**Where the body rests.** In the side-view bedroom, the mattress runs from
y≈1065 (far edge, against the wall) to y≈1270 (near edge). Every pose that was
accepted rests at **y 1084-1126** — the back of the mattress. Every pose that
was rejected rested at **y 1217-1235**, on the front lip, right where the
mattress rolls into its vertical face, and read as "perched on the edge" and
"not settled". Adjusting this by eye burned two rejected rounds; computing it
landed within 1-4 px.

With `--bbox` the silhouette is **centred** in the box with padding, so:

```
cw = round(size*1.5/8)*8          ch = cw*832/1216 rounded to /8   (--horizontal)
                                  ch = cw                          (square box)
base_y = y*1.5 + ch - padding     (padding measured on the previous run, scaled)
```

Careful: this assumes the **top of the silhouette is the head**. With the arms
raised it is not, and the head slides down out of the pillow while the box
still lands exactly where it was told to.

**Whether the cutout is a whole body.** Aspect ratio (width/height) of a
lying figure:

| | aspect |
|---|---|
| whole body, lying, accepted | **2.4 - 2.5** |
| upper body only (an amputated cutout) | **~1.6** |

A cutout at 1.6 is head, shoulders and a slab of blanket that ends dead, with
no hips, legs or feet. **Shrinking it does not fix it, it only hides it.**

**But metrics are not enough — look at the image.** One run scored aspect 2.57,
base y 1087 and zero colour cast, all on target, and had **two heads** and read
as nude. Measure before showing; never instead of looking.

---

## Texture and sharpness

Metric: high-frequency energy (mean absolute difference between neighbouring
pixels), which is what reads as detail.

| variant | texture | vs reference |
|---|---|---|
| base 1024, style LoRA 0.5 | 4.78 | — |
| upscale denoise 0.35 instead of 0.2 | 4.53 | **−5 %** |
| base resolution 1216 instead of 1024 | 4.03 | **−16 %** |
| style LoRA 0.25 | 4.80 | +0 % |
| **style LoRA 0.0** | **5.97** | **+25 %** |
| **style LoRA 0.0 + cfg 4.0** | **6.42** | **+34 %** |

### Dead ends — do not retry

- **Raising the upscale denoise does not add texture.** From 0.2 to 0.35 it
  changes 0.28 % of pixels and comes out *waxier*. `UltimateSDUpscale` is
  already tiled (1024 tiles = SDXL native), and splitting the image into 4
  quadrants by hand would push each tile *above* native, which is where this
  model starts duplicating elements.
- **Raising the base resolution makes it worse.** The model spreads the same
  detail over more pixels.
- **The style LoRA at half strength is the worst of both worlds**: no texture
  gain at all, and the highest saturation of any variant.
- **The hands pass on these poses.** Measured, not guessed — see below.
- **The `beta` scheduler does not buy back the skin texture.** The theory was
  sound and wrong: `beta` spends more steps in the low-noise tail, where
  surface detail gets resolved, so it should read as paint rather than
  airbrush. Measured on 10 seeds per arm, bare text2img at 1024, same seeds
  and prompt, everything else held at the current baseline:

  | scheduler | texture | vs baseline | saturation | s/img |
  |---|---|---|---|---|
  | `karras` (baseline) | 6.62 | — | 69.3 | 29 |
  | `beta` (alpha 0.6 / beta 0.6) | 6.37 | **−4 %** | 68.8 | 18 |
  | `beta57` (alpha 0.5 / beta 0.7) | 6.48 | **−2 %** | 67.9 | 17 |

  Both beta arms come out *below* karras. The schedule is not the lever; the
  style LoRA is (see the table above). Reproduce with
  `python scripts/ab_modelo.py --arms wai,wai_beta,wai_beta57 --seeds 10`.
  `beta` needs `BetaSamplingScheduler` + `SamplerCustom` instead of `KSampler`,
  which is why those arms build a different graph.

  These numbers are ~6, not the ~4.8 of the table above, because the table is
  the full scene pipeline (collage, detail pass, upscale) and this is bare
  text2img. Same metric, different content: only the deltas inside one run
  compare.

- **Another model does not buy the texture either.** Anima (`anima-aesthetic-v1.0`,
  a 2B DiT finetune of Cosmos-Predict2-2B-Text2Image) was measured against WAI
  on the same 10 seeds, bare text2img at 1024, each arm on its own recommended
  sampler/scheduler/steps/cfg. It lands *below* the WAI baseline:

  | arm | texture | vs WAI | skin only | saturation | s/img |
  |---|---|---|---|---|---|
  | `wai` (style LoRA 0.5, baseline) | 6.62 | — | 2.06 | 69.3 | 22 |
  | `wai_beta` | 6.37 | −4 % | 1.79 | 68.8 | 17 |
  | `wai_beta57` | 6.48 | −2 % | 1.90 | 67.9 | 17 |
  | **`wai_nolora` (style LoRA 0.0)** | **9.65** | **+46 %** | **3.05** | 74.5 | 17 |
  | `anima` | 6.34 | −4 % | **1.17** | 78.8 | 32 |

  s/img is the median, so the one cold start per arm is excluded; Anima is
  genuinely ~2x slower per image because the worker has 16 GB and cannot hold
  SDXL and Anima at once, so it reloads a ~5 GB model on every request.

  Anima is the *worst* arm on skin and the most saturated. It draws cleanly —
  harder line art, more separated hair strands, cheek hatching — which is why
  its first seed came in at 7.37 and looked like a win. That seed was an
  outlier; over 10 it is 6.34. Do not re-run this comparison hoping the first
  number was real.

- **Confirmed at n=10: dropping the style LoRA is the only lever that works.**
  The +25 % in the table above understates it; on 10 bare text2img seeds it is
  **+46 %**, and it is the only arm that moves skin as well as the whole frame.
  The documented colour cost does not reproduce at this scale: saturation goes
  69.3 -> 74.5, **+7.5 %**, not the +56 % of the single `s121212` pair. A
  texture gain that costs 7 % saturation is affordable in a way that one
  costing 56 % is not, so the "paid for in colour" framing in the older note is
  too pessimistic for bare generation. It may still hold through the full
  pipeline, which is where that pair was measured — untested.

  Caveat: the no-LoRA arm also composes *busier scenes*
  (window mullions, picture frames, shirt buttons, far more hair strands), so
  part of +46 % is scene complexity, not surface. The skin-only column exists
  to control for exactly that and still favours it, 3.05 vs 2.06.

  **On the "skin only" column.** The headline metric is a blunt
  neighbouring-pixel difference over the whole frame, so across models with
  different drawing styles it partly counts line art. The skin-only figure
  scores every 64 px patch, keeps the skin-coloured ones (HSV hue 3-30,
  saturation 25-120, value >= 110) and averages the flattest quartile — surface
  micro-detail with the lineart outliers dropped. It was built to check whether
  Anima's lead was an artefact of style. It was not needed for that in the end:
  both metrics rank all five arms identically. Keep it for cross-model
  comparisons; inside one model the cheap whole-frame number is sufficient.

  Reproduce the whole table with:

  ```powershell
  python scripts/ab_modelo.py --arms wai,wai_beta,wai_beta57 --seeds 10
  python scripts/ab_modelo.py --arms wai_nolora --seeds 10 --out output/ab-model/nolora
  python scripts/ab_modelo.py --arms anima --seeds 10 --out output/ab-model/anima
  python scripts/skin_texture.py output/ab-model/wai/wai_1*.png
  python scripts/skin_texture.py output/ab-model/nolora/ output/ab-model/anima/
  ```

  `ab_modelo.py` rewrites `metrics.json` and `ab_model.png` from its own run's
  rows only, so every arm set needs its own `--out` or it erases the previous
  one.

### What works

`stuffy_ai_style_ilxl_v2_goofy` at 0.5 — inherited by both `BASE`
(construir_wf) and `wf_sprite.json` — is what flattens the skin. At 0 the
render gains contrast, defined speculars and cleaner line art.

The catch: it is **not a post-process**. Dropping it changes composition,
framing and hair colour, and raises mean saturation from 0.326 to 0.507
(+56 %) with an amber cast. Lowering cfg from 5.5 to 4.0 helps on *both* axes
(more texture, less saturation) but does not close the gap.

The colour is fixed **in post, for free**, by `scripts/una_pasada.py --balance`:
channel gains equalised over the brightest 20 % of pixels (the sheets, which
should be neutral) plus a pull on saturation. Detail is untouched because it is
a colour transform.

---

## Negatives: overloading one makes it worse in both directions

Three rounds changing only the scene negative:

| | result |
|---|---|
| no scene negative | neutral colour, correct |
| ~10 terms at weight **1.5-1.6** | whole render tinted **acid yellow** (blue channel 19-57 vs R/G 145-188), **and the person it was negating still appeared** |
| sober negative, weights only where earned | neutral, and it blocks |

Counterintuitive, because the reflex when something leaks through is to raise
the weight. For "empty bed" it pays far better to push from the **positive**
(`(empty bed:1.3), nobody, unoccupied bed`) than to negate `person`.

Note: `--scene-neg` (node 11) did not exist until this session. The scene was
the only one of the three passes with no editable negative.

---

## Two silent failures that were fixed

Both share a shape: the graph does not error, it just quietly does the wrong
thing.

**The ControlNet skeleton was misaligned in every run of the project.**
`with_pose` built nodes 75/76/77 with the *default* geometry (a 640x640
skeleton at 200,380 over a 1024 canvas) and nobody rewrote them. Since this
project always runs `--canvas 1536` with its own `--x/--y/--size`, the skeleton
landed somewhere else, at another scale, and the fusion was guided towards a
pose that was not there. Visible in `output/bedroom/fix4d`: `e_pose` is
1024x1024 while the composite is 1536x1536. Fixed by reading node 26 — the
character already resized into the destination box, after `--bbox` and
`--flip` — and rewriting 75/76/77 from the `g` that `place_box` returns.

Rule: anything that depends on where the character goes must read `place_box`'s
`g`, never recompute it.

**`DETAIL_POS` carried the character's description.** It said
`long red hair, (blue eyes:1.3), detailed eyes` — Asuka, hardcoded in a generic
pipeline. FaceDetailer (node 95) reads that same prompt from node 93, so *both*
passes that touch the face pushed the same way. On a pose with her eyes closed
the face came out correct in the collage, was smudged by the fusion at denoise
0.55, and was then rebuilt **with an open blue eye** two passes later. It is
invisible unless you zoom into the face of the final upscale. Identity now
travels in `--detail-prompt`, which reaches both passes.

---

## The hands pass: closed, with the measurement

The question was whether `--hands yolo` did nothing because the threshold was
too high. Lowering it does not help: it turns a silent no-op into a wrong
detection.

Probe on pose `1_edge`, 1536x1536, `--hands yolo --hands-thr 0.25`, no upscale
(`output/bedroom/hands_thr/`):

| | |
|---|---|
| `i_hands` mask | 5537 px, one box, (336,768)-(448,816) |
| what is in that box | empty wall |
| where the hands actually are | around (650,930)-(790,1060) |

So the pass ran and repainted a patch of background. At the node default (0.4)
the detector marks nothing and `FaceDetailer` returns the image untouched; at
0.25 the only thing it marks is a false positive. There is no threshold in
between that lands on the hands, because the detector is not seeing them at
all: `hand_yolov8s` is trained on photographs and this art style costs it the
confidence, exactly like MeshGraphormer above.

The five bedroom poses re-rendered with `--hands yolo` and came back changed in
the same 5523-5545 px patch of wooden floor in all five. Five different poses
cannot change identically in the same spot from a hands pass — that was
run-to-run GPU noise, and the detector never fired.

Rule: the hands on these renders are fixed by `hd2` (whole-body re-diffusion at
1536), which reaches them, or not at all. Do not spend a run on `--hands`.
`_yolo_false_positive.png` in that folder draws both boxes over the render.

---

## Composite or single pass?

`escena.py` (scene + cutout + paste) exists to put a **small** figure into a
scene. It pastes on top, so there is no occlusion: nothing in the scene can
pass in front of the character.

When the figure **fills the frame** there is no background left to preserve and
the paste only costs a cutout seam, no occlusion, and anatomy generated at
832x1216 and then rescaled. `scripts/una_pasada.py` renders both together in
one shot: better anatomy, no seam, and the model resolves contact with the bed
by itself.

---

## Removed

`--rot90` (generate the character upright and lay it down with an exact quarter
turn) guaranteed a horizontal body axis to the pixel — the model draws lying
figures on a ~19° diagonal even on a 1216x832 canvas, and the worker has no
arbitrary-angle rotation node (`ImageRotate` only does 90/180/270). It worked
mechanically and read as a standing person levitating head-down, which is worse
than the diagonal. Removed after it went unused in every pose. The fix that did
work was giving the silhouette a **flat base** (allow the pillow in the
character prompt) — see `bedroom-poses.md` if that file exists, or the reasoning
above about where the body rests.
