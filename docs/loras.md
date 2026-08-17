# Character LoRAs

Notes for testing character LoRAs on top of `waiIllustriousSDXL_v170`.

---

## Before downloading anything: try it without a LoRA

`waiIllustrious` is based on **Illustrious**, which is trained on Danbooru
tags and **already knows many characters natively**. `souryuu asuka langley`
is a well-represented tag.

In this session's tests, sprites came out with a perfectly recognizable Asuka
**without any LoRA**, using only:

```
(souryuu asuka langley:1.3), (asuka langley:1.2), neon genesis evangelion,
long red hair, ahoge, (blue eyes:1.3), two side up, hair tubes, hair ribbons
```

What drifted between frames **was not the face, it was the clothes**: the
t-shirt turned into a dress at high denoise, and the footwear changed from
frame to frame.

Before adding a LoRA, try **reinforcing the outfit tags with weights**. In
the tests, switching from `white sneakers` to `(white sneakers:1.2)` plus
`barefoot, black shoes, boots` in the negative solved the footwear
inconsistency completely, and it costs zero.

A character LoRA will serve you mostly if you need:
- A specific design (e.g. *Rebuild* vs the original series).
- To fix a specific outfit that tags do not describe well.
- To push denoise above 0.85 without the identity drifting away.

---

## Candidates for Illustrious

Neither is tested in this project yet. The data is what their pages declare.

### 1. Souryuu Asuka Langley — Rebuild of Evangelion

<https://civitai.com/models/1259885/souryuu-asuka-langley-rebuild-of-evangelion-sdxl-lora>

| | |
|---|---|
| File | `asuka_rebuild_v2-illustri` |
| Size | 54.81 MB |
| Base | Illustrious |
| Recommended strength | 1.0 |
| Published | 2025-02-19 |

Declared activation tags:

```
1girl, souryuu asuka langley, rebuild of evangelion, blue eyes, brown hair,
interface headset
```

More outfit-specific tags (school uniform, plugsuit variants).

> Note the `brown hair` in the triggers: it is the *Rebuild* design, not the
> original series' orange hair. If you are after the classic look, keep that
> in mind or override it with a weighted `long red hair`.

### 2. Rebuild of Evangelion — Asuka Langley Sohryu

<https://civitai.com/models/2226332/rebuild-of-evangelion-asuka-langley-sohryu>

| | |
|---|---|
| Size | 109.15 MB |
| Base | Illustrious |
| Trigger | `Asuka` |
| Recommended strength | 0.8 |
| Clip skip | 2 |
| Published | 2025-12-15 |

> The `clip skip 2` it asks for **matches the `-2` we already use** in the
> `CLIPSetLastLayer` node (node `60`). Nothing needs to change.

---

## How to add a LoRA to the pipeline

### 1. Mirror it in R2

Download it from Civitai and upload it to the bucket:

```powershell
python -c "import sys; sys.path.insert(0, 'scripts'); import boto3, config; \
  e = config.r2_credentials(); \
  boto3.client('s3', endpoint_url=e['S3_ENDPOINT_URL'], \
    aws_access_key_id=e['S3_ACCESS_KEY_ID'], \
    aws_secret_access_key=e['S3_SECRET_ACCESS_KEY'], region_name='auto') \
  .upload_file('asuka_rebuild_v2-illustri.safetensors', e['S3_BUCKET_NAME'], \
    'comfy-stack/models/loras/asuka_rebuild_v2-illustri.safetensors')"
```

### 2. Add it to the provisioning

One line in the `MODELS` array of `serverless_provision.sh`:

```bash
MODELS=(
  ...
  "comfy-stack/models/loras/asuka_rebuild_v2-illustri.safetensors|loras/asuka_rebuild_v2-illustri.safetensors"
)
```

And deploy:

```powershell
python scripts/renew_provisioning.py --update-workers
```

> **Space**: with 16 GB of disk ~3 GB stay free after ControlNet. A 55-110 MB
> LoRA fits easily, but if you are adding several, raise `VAST_DISK_SPACE`
> to 20 in `.env`.

### 3. Chain it in the workflow

The workflows already have a style `LoraLoader` at node `2`. A character LoRA
**chains** after it: its `model` and `clip` come from node `2`, and everything
that consumed `2` switches to the new node.

```json
"2b": {
  "class_type": "LoraLoader",
  "inputs": {
    "lora_name": "asuka_rebuild_v2-illustri.safetensors",
    "strength_model": 1.0,
    "strength_clip": 1.0,
    "model": ["2", 0],
    "clip": ["2", 1]
  },
  "_meta": { "title": "character LoRA" }
}
```

Then re-point the consumers:

- `CLIPSetLastLayer` (node `60`): `clip` goes from `["2", 1]` to `["2b", 1]`
- All `KSampler`s: `model` goes from `["2", 0]` to `["2b", 0]`
- `FaceDetailer` (if used): same `model` change

In `sprites.py` and friends the sampler is node `23`; in the scene ones they
are `13`, `43` and `88`.

---

## Test protocol

To know whether the LoRA adds anything, isolate the variable:

1. Generate a batch **without** the LoRA with `sprites.py`, fixed seed.
2. Generate the same batch **with** the LoRA, same seed and same prompts.
3. Compare on the two things that failed: **outfit consistency between
   frames** and **drift at high denoise**.

Sweep the strength: `0.6 / 0.8 / 1.0`. An over-strong character LoRA tends
to also impose the pose and framing, which is exactly what you do not want
when the pose is controlled by ControlNet.

And watch out for a real conflict: if the character LoRA fights the style
LoRA at node `2`, lower the style one from `0.5` to `0.3` before touching
the character one.
