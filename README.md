# ComfyUI serverless on Vast.ai

Generates images with `workflows/wf.json` (waiIllustrious SDXL v170 + stuffy
LoRA + FaceDetailer + UltimateSDUpscale) on a serverless Vast.ai endpoint.
Models and custom nodes are provisioned from Cloudflare R2 and images are
uploaded back there.

> **Setup**: everything deployment-specific (Vast identifiers, bucket, R2 URL,
> credentials) lives in `.env`, which is **not versioned**. Copy `.env.example`
> to `.env` and fill it in. In the docs, values appear as `$VAST_ENDPOINT_ID`,
> `$VAST_TEMPLATE_ID`, etc.

## Layout

| Path | What it is |
|---|---|
| `scripts/call_endpoint.py` | Client. Sends `wf.json` to the endpoint and returns the image URL. |
| `scripts/construir_wf.py` | **Builds the scene pipeline graph** (poses, detail passes, hands, upscale). |
| `scripts/escena.py`, `frames.py`, `correr.py` | Runners for the scene pipeline. |
| `scripts/una_pasada.py` | Single-pass render: character and scene generated together, no cutout and no paste. Better anatomy and no seam; use it when the figure fills the frame. |
| `scripts/sprites.py`, `normalizar.py`, `ciclo_pose.py` | RGBA sprite generation and normalization. |
| `scripts/ab_detalle.py`, `ab_resolucion.py`, `hoja12.py` | A/B comparison harnesses. |
| `scripts/serverless_provision.sh` | Worker provisioning. Copy of what runs in R2 (`comfy-stack/scripts/serverless_provision.sh`). |
| `scripts/renew_provisioning.py` | Uploads the provisioning to R2, regenerates the presigned URL and updates template + workergroup. |
| `scripts/sondear_nodos.py` | Dumps the real node schema from the live worker (`object_info` over SSH). For wiring a new node without guessing. |
| `scripts/validar_wf.py` | Validates every flag combination against the worker schema. |
| `scripts/visor.py` | Generates `output/visor.html`, a self-contained A/B viewer. |
| `webapp/` | Local web console for the endpoint (FastAPI + one static page). Holds the API key server-side, submits the same `/generate/sync` request the CLI does, and streams phase progress over SSE. |
| `workflows/` | All workflows in ComfyUI **API** format (what is sent in each request). |
| `docs/` | Parameter recipe and prompt findings (`reproducing-results-from-scratch.md`), character LoRAs (`loras.md`), and what each lever actually does plus the dead ends not worth retrying (`levers-and-dead-ends.md`). |
| `output/` | Everything generated, organized by experiment, each with its own `README.md`. |
| `.env` | Configuration and credentials. **Not versioned, do not share.** |
| `output/general/last_response.json` | Full response of the last request (written by the client). |

---

## Quick start

```powershell
cd <your-repo>

# quick, no upscale
python scripts/call_endpoint.py --no-upscale --steps 20 `
  --prompt "aetherion, solo, 1girl, long red hair, (blue eyes:1.3), looking at viewer, standing"

# full workflow: FaceDetailer + UltimateSDUpscale 2x -> 2048x2048
python scripts/call_endpoint.py --steps 30 --cfg 6 --seed 552827645330068 `
  --prompt "..." --negative "clothes, red eyes, bad hands, ..."
```

The image URL is printed to stdout and the whole response is saved to
`output/general/last_response.json`.

### Or from the browser

```powershell
python webapp/server.py        # http://127.0.0.1:8800
```

Same endpoint, same `wf.json`, same flags — the page is only a front end for
`build_workflow`. The API key stays in the server process; the browser never
sees it.

What it shows is **phase** progress, not a percentage. The pyworker only
exposes `/generate/sync` and `/health`, there is no per-step callback, and
ComfyUI's own port is not published, so a real progress bar is impossible
without changing the template. Phases come from polling the Vast API for the
worker's state, which is where the time actually goes anyway: a cold start is
~7 min and the render is ~18 s.

It also runs the stranded-worker check (`unstick`, below) before submitting,
so a job that would otherwise sit at "queued" for the whole timeout turns into
a cold start on a different machine instead.

### Flags

| Flag | Default | Notes |
|---|---|---|
| `--prompt` | the one in `wf.json` | Positive prompt (node `3`). |
| `--negative` | the one in `wf.json` | Negative prompt (node `5`). |
| `--seed` | random | Always printed, so results can be reproduced. |
| `--width` / `--height` | 1024 / 1024 | Native latent. 1024x1024 is the most stable. |
| `--batch` | 1 | Images per request. |
| `--steps` | the one in `wf.json` (35) | Also adjusts `end_at_step`. |
| `--cfg` | the one in `wf.json` (5.5) | Above 6.5 the eyes degrade. |
| `--no-upscale` | off | See note below. |
| `--workflow` | `workflows/wf.json` | To use another workflow. |
| `--cost` | 100 | Units the autoscaler discounts per request. |
| `--timeout` | 900 | Seconds. Includes cold start. |
| `--out` | `output/general/last_response.json` | Where to dump the response. |
| `--remove-bg` | off | Cuts the character out with BiRefNet (node 70, model with `--rmbg-model`). |

**About `--no-upscale`:** ignoring the upscaler nodes is not enough. The
`SaveImage` (node `7`) must be re-hung directly from the FaceDetailer and
nodes `46` and `47` must be **deleted**, or ComfyUI executes them anyway
because they are still in the graph. The script already does this.

---

## From your own code

```python
import asyncio, json, uuid
from pathlib import Path
from vastai import Serverless

async def generate(workflow: dict) -> str:
    key = (Path.home() / ".config/vastai/vast_api_key").read_text().strip()
    client = Serverless(api_key=key)          # explicit, see trap below
    try:
        ep = await client.get_endpoint(name=ENDPOINT_NAME)
        r = await ep.request(
            "/generate/sync",
            {"input": {"request_id": str(uuid.uuid4()), "workflow_json": workflow}},
            cost=100, timeout=900,
        )
        return r["response"]["output"][0]["url"]
    finally:
        await client.close()

wf = json.loads(Path("workflows/wf.json").read_text(encoding="utf-8"))
wf["3"]["inputs"]["text"] = "your prompt"
wf["27"]["inputs"]["value"] = 12345           # seed
print(asyncio.run(generate(wf)))
```

> **SDK trap:** `Serverless.__init__` has
> `api_key = os.environ.get("VAST_API_KEY", None)` as a *parameter default*,
> so it is evaluated **when the module is imported**. If you set the
> environment variable from Python before instantiating, it is ignored.
> Either export it in the shell before launching the process, or pass
> `api_key=` by hand.

### Payload contract

```json
{"input": {"request_id": "<uuid>", "workflow_json": { ...ComfyUI API format... }}}
```

Optional keys inside `input`: `s3` (overrides the upload destination) and
`webhook` (async response instead of blocking).

### Nodes of `workflows/wf.json`

| Node | What it is |
|---|---|
| `1` | CheckpointLoaderSimple — `waiIllustriousSDXL_v170.safetensors` |
| `2` | LoraLoader — `stuffy_ai_style_ilxl_v2_goofy.safetensors` |
| `3` / `5` | Positive / negative prompt |
| `60` | CLIPSetLastLayer — **clip skip `-2`**, critical: with `-1` images come out black |
| `16` | KSamplerAdvanced — `steps`, `cfg`, `sampler_name`, `scheduler` |
| `17` | FaceDetailer (Impact-Pack) |
| `20` | UltralyticsDetectorProvider — `bbox/face_yolov8m.pt` |
| `27` | Seed |
| `39` / `40` / `41` | Width / height / batch |
| `46` / `47` | UpscaleModelLoader + UltimateSDUpscale (`4x_NMKD-Siax_200k.pth`) |
| `7` | SaveImage — where the api-wrapper grabs the image from |

---

## Measured performance (RTX 5080)

| Configuration | Time |
|---|---|
| 832x1216, 12 steps, no upscale | 8.3 s |
| 1024x1024, 20 steps, no upscale | 13.4 s |
| 1024x1024, 30 steps, FaceDetailer + 2x upscale -> 2048x2048 | 32.6 s |
| Cold start (stopped worker -> first request) | ~2 min |

---

## How it is wired up

```
endpoint <name> ($VAST_ENDPOINT_ID)
  └── workergroup $VAST_WORKERGROUP_ID
        └── template "<name> ComfyUI Serverless" (id $VAST_TEMPLATE_ID)
              image: vastai/comfy:v0.30.0-cuda-13.2-py312
              runtype: ssh          <- NOT jupyter (see Troubleshooting)
              env: -p 3000:3000 (pyworker)
                   -e COMFYUI_ARGS="--disable-auto-launch --port 18188"
                   -e HF_TOKEN=...
                   -e PROVISIONING_SCRIPT=<R2 URL>
              onstart:
                   export SERVERLESS=true BACKEND=comfyui-json
                   entrypoint.sh &                      <- starts supervisord
                   start_server.sh | bash               <- starts the pyworker
```

The `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_BUCKET_NAME`,
`S3_ENDPOINT_URL` and `S3_REGION` credentials are **account environment
variables** in Vast (`vastai show env-vars`), not in the template. Vast
injects them into every worker.

`serverless_provision.sh` runs via `PROVISIONING_SCRIPT` **before** the worker
is marked ready. It installs Impact-Pack, Impact-Subpack and
UltimateSDUpscale, and pulls these 4 models from R2:

```
comfy-stack/models/checkpoints/waiIllustriousSDXL_v170.safetensors   6.9 GB
comfy-stack/models/loras/stuffy_ai_style_ilxl_v2_goofy.safetensors   114 MB
comfy-stack/models/ultralytics/bbox/face_yolov8m.pt                   52 MB
comfy-stack/models/upscale_models/4x_NMKD-Siax_200k.pth               67 MB
```

If the script fails it exits with code != 0 and the worker is **not** marked
ready, so provisioning errors show up as explicit errors in the logs instead
of a silent timeout.

### If you change the provisioning

```powershell
python scripts/renew_provisioning.py                    # uploads to R2 + updates template
python scripts/renew_provisioning.py --update-workers   # also forces the live workers
```

It does the whole cycle: normalizes line endings to LF, uploads
`serverless_provision.sh` to R2, checks the public URL serves exactly that,
updates the template and re-points the workergroup to the resulting hash
(which changes on every update).

`PROVISIONING_SCRIPT` points to the bucket's **Public Development URL**, which
is permanent:

```
https://pub-XXXXXXXXXXXX.r2.dev/comfy-stack/scripts/serverless_provision.sh
```

With `--presigned` it instead generates a 7-day signed URL, in case the
bucket's public access is ever disabled.

It reads the R2 credentials from `.env` or the environment. Vast masks the
values in `vastai show env-vars` even with `-s`, so they cannot be recovered
from there.

> Cloudflare returns **403** to urllib's default User-Agent. `curl` and
> `wget` pass fine, which is what the worker provisioner uses; the
> verification script impersonates curl. If you write your own tooling
> against that URL, remember to send a User-Agent.

> Changing the template triggers a worker replacement: the autoscaler spins
> up a new one and provisions from scratch (a few minutes and a few cents).
> That is normal; it settles on a cold worker on its own.

---

## What can be changed and how

There are three layers, and touching them costs very differently. The higher
up, the cheaper.

| I want to change | Where | Command | Cost |
|---|---|---|---|
| Prompt, seed, size, steps, cfg, batch | client flags | `python scripts/call_endpoint.py --...` | nothing |
| The workflow graph (nodes, connections, another whole workflow) | `workflows/wf.json` or `--workflow another.json` | `python scripts/call_endpoint.py --workflow another.json` | nothing |
| Models, LoRAs, custom nodes | `CONFIGURATION` block of `serverless_provision.sh` | `python scripts/renew_provisioning.py --update-workers` | ~5 min of reprovisioning |
| Docker image, ports, env, GPU, disk | constants at the top of `renew_provisioning.py` | `python scripts/renew_provisioning.py` | worker replacement |
| How many workers and when | endpoint | `vastai update endpoint $VAST_ENDPOINT_ID ...` | immediate |

### The workflow does not require redeploying

This is the important part: **the workflow travels in every request**. The
worker has no copy of `wf.json`; whatever is sent in `input.workflow_json` is
what gets executed. You can change the whole graph, use another workflow, or
send different workflows in consecutive requests without touching the
deployment.

The only limit is that the models and nodes that workflow uses must exist on
the worker. Otherwise ComfyUI fails naming the missing one.

### Turning worker features on and off

At the top of `serverless_provision.sh` there is a `true`/`false` toggle per
feature. Each one drags **its own models, custom nodes and pip packages**, so
turning off what you do not use saves disk and cold start time:

| Toggle | Client flag | Size | State |
|---|---|---|---|
| `FEAT_UPSCALE` | `UltimateSDUpscale` in `wf.json` | 67 MB | `true` |
| `FEAT_RMBG` | BiRefNet, cuts out the character | ~1 GB | `true` |
| `FEAT_CONTROLNET` | required by any `--pose` and `--hands mesh` | 2.5 GB | `true` |
| `FEAT_POSE_DWPOSE` | `--pose dwpose` | 351 MB | `true` |
| `FEAT_POSE_OPENPOSE` | `--pose openpose` | ~430 MB | **`false`** |
| `FEAT_FACE` | `--face` | 52 MB | `true` |
| `FEAT_HANDS_YOLO` | `--hands yolo` | 22 MB | `true` |
| `FEAT_HANDS_MESH` | `--hands mesh` | 1.37 GB | `true` |

`FEAT_POSE_OPENPOSE` is `false` because `OpenposePreprocessor` fails on anime
and worsens the pose — see the [What works and what does not](#what-works-and-what-does-not-all-measured)
table. Turning it off saves the three annotators of `lllyasviel/Annotators`.

> ⚠️ Turning a toggle off **does not change the workflows**. If you send a
> workflow that uses something disabled, ComfyUI fails with the name of the
> missing model or node.

**Two things that used to hang off a download in the middle of a request**
and are now pre-downloaded at provisioning time:

- The **DWPose** models (`yolox_l.onnx` + `dw-ll_ucoco_384.onnx`, 351 MB) were
  not in any array; the node downloaded them from HuggingFace on the first
  request. Found while inventorying a live worker: they were on disk without
  being in the provisioning.
- `mediapipe` and `trimesh`, which the MeshGraphormer node installs **via
  pip** if it does not find them, also mid-request.

> 🐛 **Careful when adding toggles**: a `[ -n "$k" ] && echo ...` as the last
> statement of a `for` makes the whole `$( ... )` substitution exit with code
> 1 when the array ends up empty, and with `set -euo pipefail` that **kills
> the provisioning** and the worker never gets marked ready. Use
> `if ... then ... fi`. Loops over arrays also carry `"${ARR[@]:-}"` and a
> guard `continue`.

### Adding a model or LoRA

1. Upload it to R2 under `comfy-stack/models/<type>/`.
2. Add a line to the `MODELS` array of `serverless_provision.sh`:

   ```bash
   MODELS=(
     ...
     "comfy-stack/models/loras/my_lora.safetensors|loras/my_lora.safetensors"
   )
   ```

   The format is `<bucket key>|<relative path inside models/>`.
3. `python scripts/renew_provisioning.py --update-workers`

Already-downloaded models are skipped by comparing size with R2, so adding a
new one does not re-download the 7 GB.

### Testing a model without reprovisioning

For **tests**, touching `serverless_provision.sh` is not worth it: that
triggers a worker replacement, a cold start and re-downloading the ~7 GB. The
model is downloaded straight to the live worker over SSH:

```bash
ssh -i ~/.ssh/xcl -o IdentitiesOnly=yes -p <port> root@<ip>
curl -sL -o /workspace/ComfyUI/models/ultralytics/bbox/hand_yolov8s.pt \
  https://huggingface.co/Bingsu/adetailer/resolve/main/hand_yolov8s.pt
```

**ComfyUI picks up the new file without a restart**; verify with
`curl -s http://127.0.0.1:18188/object_info/UltralyticsDetectorProvider`.

The port and IP come from `vastai show instances --raw` (field `ports`); note
that the `ssh_host`/`ssh_port` the CLI shows is the proxy, and the direct
mapping of 22 is usually a different one. The key that works is `~/.ssh/xcl`.

> Two dead ends already checked: `vastai attach ssh` on an instance that is
> **already running** returns `success` but **does not propagate the key**,
> and `vastai execute` only works on **stopped** instances.

> ⚠️ What is downloaded this way **does not survive a worker replacement**.
> Once the test convinces you, make it permanent and run
> `renew_provisioning.py --update-workers`.

To make it permanent there are three spots in `serverless_provision.sh`, all
hanging off the toggle of their feature:

- **`MODELS`** / **`EXTRA_FILES`** — files mirrored in R2. They verify size
  against the origin and skip if already present.
- **`URL_FILES`** — direct URL download, for public HuggingFace weights not
  worth mirroring. Format `<url>|<path relative to COMFY_DIR>`. Only skipped
  if the file already exists; does not compare sizes.

What the hand variants need, if they are to be made permanent:

| Path | Files | Size |
|---|---|---|
| `hd3y` (yolo) | `Bingsu/adetailer` -> `hand_yolov8s.pt` | 22 MB |
| `hd3m` (MeshGraphormer) | `hr16/ControlNet-HandRefiner-pruned` -> `graphormer_hand_state_dict.bin` + `hrnetv2_w64_imagenet_pretrained.pth` | 856 + 513 MB |

The third file of that repo, `control_sd15_inpaint_depth_hand_fp16.safetensors`
(722 MB), is **not needed**: it is the SD1.5 ControlNet and here the SDXL
union in depth mode is used. `mediapipe` and `trimesh` are already in the
image; if they were not, the node would pip-install them **mid-request**.

### Adding a custom node

One line in the `NODES` array, format `<repo>|<directory>|<recursive>`:

```bash
NODES=(
  ...
  "https://github.com/whatever/ComfyUI-Something.git|ComfyUI-Something|"
)
```

The third field is only filled (with `recursive`) if the repo has submodules,
like `ComfyUI_UltimateSDUpscale`. If the node ships a `requirements.txt` it
installs itself; if it needs extra packages, add them to `PIP_EXTRA`.

### Changing the GPU, the image or the ports

They are constants at the top of `renew_provisioning.py`: `IMAGE`, `IMAGE_TAG`,
`SEARCH_PARAMS`, `HF_TOKEN`, `ONSTART`. Edit and run `renew_provisioning.py`.
For example, to also accept 4090s:

```python
SEARCH_PARAMS = "gpu_name in [RTX_5080,RTX_4090] disk_space>=60 dph_total<0.40 verified=true rentable=true"
```

Careful going below 16 GB of VRAM: the workflow with FaceDetailer and upscale
to 2048 does not fit comfortably.

### Changing the benchmark workflow

`BENCHMARK_CKPT` and the `BENCH_JSON` block at the end of
`serverless_provision.sh`. It is kept light on purpose (512x512, 8 steps)
because it only serves for the autoscaler to measure GPU performance. If you
make it heavy, you lengthen every worker's cold start.

---

## Operations

```powershell
vastai show endpoints                    # endpoint state
vastai get endpt-workers $VAST_ENDPOINT_ID           # workers and their status
vastai get endpt-logs $VAST_ENDPOINT_ID              # autoscaler log (the most useful)
vastai get wrkgrp-logs $VAST_WORKERGROUP_ID             # workergroup log
vastai show workergroups                 # workergroup config
vastai show instances                    # live instances

# stop all spending
vastai update endpoint $VAST_ENDPOINT_ID --max_workers 0 --cold_workers 0 --min_load 0

# bring it back up
vastai update endpoint $VAST_ENDPOINT_ID --max_workers 1 --cold_workers 1 --min_load 0 --target_util 0.9
```

Current config: `max_workers=1`, `cold_workers=0`, `min_load=0`. A single
worker that stops itself when idle and is woken by the next request.

> ⚠️ **Do not set `cold_workers=1` with `max_workers=1`.** As soon as there
> is a warm worker, the autoscaler sees 0 cold workers, tries to create one,
> cannot fit in the single slot and destroys it — every 5 seconds,
> indefinitely. In the logs you see `Creating 1 workers` / `Destroying 1
> workers` in a loop. It goes unnoticed at first because a warm worker has to
> be occupying the slot.
>
> If you really want a reserved cold slot, you need `max_workers=2` (1 warm +
> 1 cold), assuming 2 GPUs rented is acceptable.

### When the worker is stranded on a full machine

A worker that stops itself when idle keeps its **disk** on the host but
releases the **GPU**. If another tenant takes that GPU before the next
request, the instance can never start again: it is pinned to that one host.
With `max_workers=1` the autoscaler does not rent a replacement either, because
from its side the slot is occupied. The symptom is a request that sits queued
until it times out, with no error anywhere.

```powershell
python scripts/mizuki.py unstick --dry-run   # diagnose, spends nothing
python scripts/mizuki.py unstick             # destroy it so a new one is rented
```

`gen` and the web console run this check automatically before submitting
(`--no-preflight` opts out). The detection is a probe, not a guess: it asks
`vastai start instance`, and only the *"required resources are currently
unavailable"* answer counts — confirmed against
`vastai search offers machine_id=<id>` returning nothing. `--dry-run` skips the
probe, because starting a startable instance is not free.

Destroying is the whole fix: the disk is re-downloaded from R2 on the new host
in the usual cold start. Note that `renew_provisioning.py --update-workers`
does **not** help here, and does not help with a disk change either — a rolling
update restarts the container on the same rented hardware, where the disk size
was fixed at rent time.

---

## Coherent scenes: character over a fixed set

Pipeline to put a character into a scene that stays identical between frames.
Everything happens **inside a single graph**: no images need to be uploaded
to the worker, which is a mess in serverless.

```
SCENE ──► KSampler(fixed seed) ─────────────► [a_scene]  <- the "set"
                                                   │
CHARACTER ──► KSampler ──► BiRefNet ──► scale ──────┤
                             │                      │
                             │              ImageCompositeMasked
                             │                      │
                       DWPreprocessor          [b_collage]
                             │                      │
                   ControlNet Union            VAEEncode
                      (openpose)                   │
                             └──────────► SetLatentNoiseMask ──► KSampler
                                                                     │
                                                                [c_fused]
                                                                     │
                                       crop -> x1024 -> re-diffuse -> paste
                                                                     │
                                                                [f_detail]
```

### Files

| | |
|---|---|
| `scripts/construir_wf.py` | **Builds the workflow**, turning each part on/off |
| `scripts/escena.py` | Runner with `--sweep` of denoise |
| `scripts/ab_detalle.py` | A/B of the second pass, with timings |
| `scripts/ab_resolucion.py` | A/B of the resolution levers (`--vertical`, `--canvas`, `--upscale`), with comparison sheets |
| `scripts/frames.py` | Pose sequence on a fixed set |
| `scripts/correr.py` | Movement sequence (character crossing the scene) |
| `workflows/wf_v_<pose>_<variant>.json` | The pre-generated matrix, for the A/B and for manual inspection |

### Switches

The whole pipeline is turned on/off with the same flags, meaning the same in
`construir_wf.py`, `escena.py` and `correr.py`:

| Flag | Values | Default | What it does |
|---|---|---|---|
| `--pose` | `none` / `openpose` / `dwpose` | `dwpose` | Pose guidance via ControlNet |
| `--detail` | `no` / `hd` / `hd2` | `hd2` | 2nd pass over the character crop |
| `--face` | flag | off | `FaceDetailer` **inside** the 2nd pass |
| `--hands` | `yolo` / `mesh` / `both` | off | Hands pass **inside** the 2nd pass |
| `--no-mask` | flag | off | Removes `SetLatentNoiseMask` (deforms the scene) |
| `--variant` | see below | — | Named shortcut |
| `--workflow` | path | — | Uses an existing `.json` and ignores everything above |

And the **character resolution** levers, orthogonal to the ones above (they
also apply when using `--variant`):

| Flag | Default | What it does |
|---|---|---|
| `--vertical` | off | Generates the character in `832x1216` (native SDXL ratio) instead of `1024x1024` |
| `--canvas N` | `1024` | Side of the scene/composition. `1536` doubles the graph's elements |
| `--upscale` | off | `UltimateSDUpscale` 2x over the final composite |
| `--bbox` | off | **Not implemented**, see below |

The **scene -> character -> collage** is the base of the pipeline and cannot
be turned off: it is what the graph itself does. What can be turned off is
the mask that protects the scene during the fusion.

```powershell
# write a concrete workflow
python scripts/construir_wf.py --pose dwpose --detail hd2 --face --hands yolo -o my_wf.json

# same thing, via the shortcut
python scripts/construir_wf.py --variant hd3y -o my_wf.json

# without writing a file: the runners build it in memory
python scripts/escena.py --pose dwpose --detail hd2 --face --hands yolo --denoise 0.85
python scripts/correr.py --variant hd3y --frames 8

# regenerate the whole matrix (3 poses x 7 variants)
python scripts/construir_wf.py --matrix
```

The `--variant` shortcuts are named combinations of the flags above:

| Variant | Equivalent to |
|---|---|
| `sd` | `--detail no` |
| `hd` | `--detail hd` (the original version, only as baseline) |
| `hd2` | `--detail hd2` |
| `hd3` | `--detail hd2 --face` |
| `hd3y` | `--detail hd2 --face --hands yolo` |
| `hd3m` | `--detail hd2 --face --hands mesh` |
| `hd3ym` | `--detail hd2 --face --hands both` |

The builder rejects impossible combinations instead of generating a broken
graph: `--face` and `--hands` live **inside** the 2nd pass, so they need
`--detail hd2`; and `--no-mask` is incompatible with the 2nd pass, because
the crop reuses that same mask (node `53`).

### What works and what does not (all measured)

| Technique | Verdict |
|---|---|
| Fixed scene + collage | Solid base |
| img2img **without** mask | ❌ **Deforms the scene** |
| img2img **with** mask | ✅ Scene intact at any denoise |
| `OpenposePreprocessor` | ❌ Fails on anime, worsens the pose |
| `DWPreprocessor` | ✅ The best pose of the three |
| Double inpaint at 1024 (`hd`) | ⚠️ Mild improvement: deformed the aspect and used the scene prompt |
| Double inpaint at 1536 with own prompt (`hd2`) | ✅ The big win. Face and hair actually drawn |
| `FaceDetailer` over the crop (`hd3`) | ✅ Fine improvement of eyes and lashes, +3.5 s |
| Identity between frames | ⚠️ Unsolved without LoRA |

### Why the mask is mandatory

`img2img` re-diffuses **the whole image**. Without a mask, raising the
denoise to integrate the character better also rewrites the scene: at 0.65
moldings appeared on the ceiling and the lamp and window frame changed. With
`SetLatentNoiseMask` limited to the character area, the rest stays intact and
you can go up to 0.85 without risk.

Two important mask parameters:

- **`GrowMask expand`**: widens beyond the silhouette. That ring is where the
  model paints the **contact shadow**. With 48, artifacts appeared on the
  wall; **24** works well.
- **`FeatherMask 32`**: blurs the edge between repainted and intact areas.

### The denoise / identity tradeoff

| denoise | Scene | Pose | Identity |
|---|---|---|---|
| 0.45 | intact | respects the collage | preserved |
| 0.55 | intact | does not fix a bad collage | preserved |
| 0.85 | intact | **repositions per the prompt** | **drifts** |

At high denoise the model reinterprets the pose inside the mask, so the
collage works as a sketch and the cutout does not need pixel-perfect
placement. The price is that clothes and face drift. **The character LoRA is
the only thing that closes that gap** — see [`docs/loras.md`](docs/loras.md),
though it is worth first trying to reinforce the outfit tags with weights,
which is free.

### Detail: the double inpaint

If the character occupies 640 px of a 1024 canvas, in the latent that is
~80x80 positions for the whole figure: face, hands and feet run out of
pixels. The second pass crops the area, enlarges it, re-diffuses and pastes
it back. Same principle as the `FaceDetailer` but for the whole body.

There are three variants, generated by `construir_wf.py`:

| | Crop enlarged to | Prompt | Denoise | Extra |
|---|---|---|---|---|
| `hd` | 1024x1024 **fixed** | the scene's (nodes `40`/`41`) | 0.35 | — |
| `hd2` | 1536 on the long side, **aspect intact** | its own crop prompt (nodes `93`/`94`) | 0.45 | — |
| `hd3` | same as `hd2` | same as `hd2` | 0.45 | `FaceDetailer` over the crop |

**`hd` gave little for three reasons, and all three were fixable:**

1. **It deformed.** The real crop is `704x676` and it was scaled to a fixed
   `1024x1024`: stretched 4% in width, re-diffused deformed and returned
   stretched. `scale_to()` in `construir_wf.py` now scales proportionally,
   rounding to a multiple of 8.
2. **It enlarged little.** 704->1024 is 1.45x; the face still measured
   ~130 px. At 1536 it is 2.2x and the face passes 300 px, which is territory
   where the model draws irises, lashes and separate strands.
3. **Wrong prompt.** It used the scene's positive
   (*"sitting on the edge of the bed, bedroom, soft window light"*). On a
   crop already closed around the character, that spends capacity repainting
   background. `hd2` uses its own prompt, character-and-detail only.

`hd3` also adds a `FaceDetailer` **after** enlarging the crop, which is where
it pays off: on the 1024 canvas the face measures ~90 px and the detector has
little to work with; on the 1536 crop it measures ~300 px. It touches 17k
pixels, all inside the face bbox.

### Hands: `hd3y`, `hd3m`, `hd3ym`

Three more variants, all on top of `hd3` and acting on the already-enlarged
crop:

| | How it finds the hand | What guides the re-diffusion | Denoise |
|---|---|---|---|
| `hd3y` | `bbox/hand_yolov8s.pt` detector | only the prompt | 0.30 |
| `hd3m` | MeshGraphormer 3D mesh | **mesh depth** via ControlNet union | 0.65 |
| `hd3ym` | both, mesh first and detector after | — | — |

`FaceDetailer` **is not face-specific**: it crops whatever the
`bbox_detector` marks. With the hand detector it does exactly the same. That
is why `hd3y` is only two nodes.

MeshGraphormer (the **HandRefiner** pipeline) fits a 3D mesh to the hand and
returns **two** outputs: the depth map and the mask of the area. It is a
**geometry guide, not a fixer**: the inpaint pass is still needed. What it
replaces is the detector, not the pass.

#### The two detectors fail differently

Measured on two scenes, one with a small, half-occluded fist and another with
a large open hand:

| | small occluded fist | large open hand |
|---|---|---|
| `hand_yolov8s` | detects | detects 2 hands, conf 0.87 |
| MeshGraphormer | **does not detect at any threshold** (0.6 / 0.3 / 0.15) | detects already at 0.6 |

> ⚠️ **MeshGraphormer fails silently.** If it does not detect, `get_depth`
> returns `None` and the node fills with `np.zeros_like`: **black depth and
> empty mask**, without error or warning. The whole pass becomes a round trip
> through the VAE that only costs time. If you use this path, always check
> the `g_depth` output: black = it did nothing.

The yolo path fails silently in exactly the same way: if the detector marks
nothing, `FaceDetailer` returns the image untouched and reports no error, so
a threshold that finds nothing is indistinguishable from a pass that ran. Its
threshold is `HANDS_BBOX_THR = 0.25` (`--hands-thr` to override) and it writes
its own `i_hands` output with the mask it actually produced: black = no hands
found. The node default is 0.4 and at that value the five bedroom poses came
back **changed in the same place in all five** — that is run-to-run GPU noise,
not a hands pass.

> ⚠️ **Lowering the threshold does not find the hands, it invents a
> detection.** Measured on pose `1_edge` at 1536x1536 (`--hands yolo
> --hands-thr 0.25`, no upscale, `output/bedroom/hands_thr/`): the mask is no
> longer black — 5537 px — but it is a single 112x48 box at (336,768)-(448,816),
> and that region is **empty wall**. The hands are around (650,930)-(790,1060).
> The pass repainted background. Above 0.4 nothing is detected; at 0.25 the
> only thing detected is a false positive. See
> `output/bedroom/hands_thr/1_edge/_yolo_false_positive.png` (red = what the
> detector marked, blue = where the hands are).

The node's default threshold (`detect_thr=0.6`) is also too strict for anime
— on the full image it detected 0 hands at 0.6 and 1 at 0.3 — so
`construir_wf.py` lowers it to `MESH_DETECT_THR = 0.3`. Even so it does not
save the occluded-fist case. It is not a wiring problem: on a real photo the
same detector finds 2 hands at 0.6.

#### What to pick

- The hand **blurry for lack of pixels** is mostly fixed by `hd2`/`hd3`
  already, because the whole-body re-diffusion at 1536 reaches it too. `hd3y`
  adds contrast and finger separation.
- The **anatomy-broken hand** (extra fingers, fused) is MeshGraphormer's
  case. But if the geometry was already correct, the depth has nothing to
  correct and only changes the shape slightly.

**Recommendation on generic scenes: `hd3y`.** It detects in both scenarios,
costs 7 s and now writes `i_hands` so the failure is not silent. Reserve
`hd3m`/`hd3ym` for when you see broken *and* clearly visible hands.

**On the bedroom poses: no hands pass.** Both detectors were run against the
real renders and neither one lands on the hands (see the two boxes above).
`hd2` is the whole pipeline that touches them, and it does so through the
whole-body re-diffusion, not through a detector. Do not spend a run on
`--hands` here.

### Character resolution: where it is lost and what recovers it

The character is generated on its own canvas, cut out with BiRefNet and
scaled into a box inside the scene. Resolution is lost in **two different
places**, and each lever attacks one:

| Where | What happens | Lever |
|---|---|---|
| At generation | A full body in square `1024x1024` only occupies a vertical strip; the rest are background pixels BiRefNet throws away | `--vertical` (`832x1216`, native SDXL ratio) |
| When pasting | The destination box measures `640` on a `1024` canvas | `--canvas 1536` |
| At the end | — | `--upscale` (UltimateSDUpscale 2x over the composite) |

All the geometry lives in **a single function**, `place_box()` in
`construir_wf.py`: it places the character box over the scene and returns the
resulting geometry, so whoever has to crop later (the 2nd pass) does not
recompute it on its own and desync. Before, this was written in two places —
`escena.py` stomped what `construir_wf.py` did — and the last write erased
the previous one.

The 8 combinations, verified on the graph (`python scripts/ab_resolucion.py`):

| combo | generation | box | canvas | nodes | box px |
|---|---|---|---|---|---|
| base | 1024x1024 | 640x640 | 1024 | 59 | 409 600 |
| `v` | 832x1216 | 440x640 | 1024 | 59 | 281 600 |
| `l` | 1024x1024 | 960x960 | 1536 | 59 | 921 600 |
| `vl` | 832x1216 | 656x960 | 1536 | 59 | 629 760 |
| `u` | 1024x1024 | 640x640 | 1024 | 62 | 409 600 |
| `vu` | 832x1216 | 440x640 | 1024 | 62 | 281 600 |
| `lu` | 1024x1024 | 960x960 | 1536 | 62 | 921 600 |
| `vlu` | 832x1216 | 656x960 | 1536 | 62 | 629 760 |

**Careful reading the px column:** in vertical the box has *fewer* pixels
(281k vs 409k) and it is still the improvement. What matters is not the box's
pixels but those falling **on the figure**: before, the silhouette occupied a
strip of a square; now it fills the box. Do not judge combos by box size.

**`--bbox` is not implemented**, and the builder raises `SystemExit` on
purpose instead of generating a silent graph. The idea: BiRefNet returns the
cutout over the whole canvas with transparent alpha, so that padding travels
to the destination box and takes a good part of the useful pixels with it.
Cropping to the silhouette makes the figure use the whole box.

Probed on the live worker (`python scripts/sondear_nodos.py`), the node that
does this is **`AILab_CropObject`** (from `ComfyUI-RMBG`, already installed
with `FEAT_RMBG`):

```
AILab_CropObject   in: image:IMAGE, mask:MASK, padding:INT   out: IMAGE, MASK
```

It is the only candidate wireable as is. The others (`CropByBBoxes`,
`ImageCropV2`) require a `BOUNDING_BOX` type that only a detector
(`rtdetr`/`sdpose`) or the manual canvas editor produces, and
`AILab_ImageCrop` crops to fixed coordinates, not to the object.

### Cost of each variant

Measured with `ab_detalle.py`, warm worker and a different seed per variant
(with the same seed ComfyUI caches the graph and the timings make no sense):

| Variant | Time | vs `sd` |
|---|---|---|
| `sd` (no second pass) | 9.0 s | — |
| `hd` | 12.5 s | +3.5 s |
| `hd2` | 18.5 s | +9.5 s |
| `hd3` | 22.0 s | +13.0 s |
| `hd3y` | 29.0 s | +20.0 s |
| `hd3m` | 35.2 s | +26.2 s |
| `hd3ym` | 43.0 s | +34.0 s |

**`hd2` is the one that pays off** and it is the default of `correr.py`: it
takes almost all the gain for 9.5 s. `hd3` adds a fine eyes-and-lashes
improvement for 3.5 s more — worth it in a single image, not so much in an
8-frame sequence.

To reproduce the comparison:

```powershell
python scripts/ab_detalle.py                                  # hd, hd2, hd3
python scripts/ab_detalle.py --variants hd3,hd3y,hd3m,hd3ym --out output/ab-detail/ab_manos
```

> ⚠️ **When timing, give each variant a different seed.** With the same seed
> ComfyUI caches nodes whose inputs do not change and timings make no sense:
> in one batch `hd3` came out at 4.5 s because it reused the whole graph of
> the previous one.

Leave `output/ab-detail/ab_detalle_full.png` (whole image) and
`output/ab-detail/ab_detalle_zoom.png` (face at 200%), which is where the
difference really shows.

---

## Costs

Two independent line items:

- **Disk**: paid **24/7**, per GB **allocated** (not used). It dominates on
  a mostly-idle endpoint.
- **GPU**: only while the worker runs.

### Disk

Measured on the real worker (2026-08-16): with `VAST_DISK_SPACE=16` the image
+ models occupied **14 GB of 16**, i.e. 2.9 GB free, and after downloading
the MeshGraphormer weights it was down to **1.6 GB (91% used)**. Too tight,
so `VAST_DISK_SPACE` went to **18**, where the worker sat around 78%
(measured 2026-09-21 on worker 51766427: **13.9 GB used of 18**).

It is now **26**, because `FEAT_ANIMA` adds a whole second model stack next to
the SDXL one: Anima is a 2B DiT with its own encoder and VAE, **5.25 GB**
across three files (3.90 + 1.11 + 0.24). At 18 GB it did not fit in the 4.1 GB
that were free. 26 leaves ~6 GB of headroom with both stacks installed.

**How much disk you ask for does not affect availability.** Pool machines
offer between 288 and 1,352 GB, so `disk_space>=16`, `>=24` or `>=60` return
exactly the same 7 offers at the same prices. The only thing that changes is
the bill, which is `storage_cost x GB`:

| `VAST_DISK_SPACE` | Cost at current price ($0.0267/GB/month) | Ceiling with `storage_cost<=0.11` |
|---|---|---|
| 16 GB | $0.43/month | $1.76/month |
| 18 GB | $0.48/month | $1.98/month |
| 24 GB | $0.64/month | $2.64/month |
| **26 GB** | **$0.69/month** | **$2.86/month** |
| 32 GB | $0.85/month | $3.52/month |

Going from 18 to 26 GB costs **21 cents a month** and loses no offer. Keep the
`disk_space>=` filter in `VAST_SEARCH_PARAMS` in sync with the number: the
filter selects machines and `--disk_space` allocates on them, so a filter
below the allocation picks machines that cannot host what is then asked of
them.

> There is no **VRAM** step between 16 and 24 GB: the market jumps from one
> to the other with nothing in between, so asking `gpu_ram>=18` is asking
> `>=24`. And there it hurts: with `storage_cost<=0.125` the cheapest 24 GB
> goes to **$0.288/h** vs $0.107/h. This is about **disk**, not VRAM.

> ⚠️ **Always check the real disk with `df -h /` on the worker before
> deciding**, do not trust this number. The README already went stale once
> (it said 32 GB when the instance had 16).

#### The `storage_cost` ceiling

It is at **`0.11`**, which at 18 GB caps the disk bill at $1.98/month. It is
not lowered further on purpose:

| `storage_cost<=` | Offers | Ceiling at 24 GB |
|---|---|---|
| 0.0625 | **1** ⚠️ | $1.13/month |
| 0.0834 | 2 | $1.50/month |
| **0.11** | **7** | **$1.98/month** |
| 0.125 (the previous) | 7 | $2.25/month |

With **a single viable offer the autoscaler relaxes the price ceiling and
rents above `dph_total`** (see [The phantom `verified=true`](#the-phantom-verifiedtrue)),
which is exactly the surprise to avoid. `0.11` keeps the same 7 offers as the
previous `0.125` and lowers the ceiling 27 cents: it costs nothing.

`storage_cost` is in **$/GB/month** and the market median is **0.20**, i.e.
$3.20/month at 16 GB (and $12/month at the 60 GB of before). The
`storage_cost<=0.0625` filter caps it at $1/month.

### Network

`inet_down_cost` is in **$/GB**. A cold start downloads ~22 GB (image +
models), so at $1.30/TB that is **~3 cents per start**. Requiring <$1/TB cuts
the range from 123 to 22 offers to save cents a month: not worth it. That is
why the filter is at `inet_down_cost<=0.005` ($5/TB).

### Hourly price

`dph_total<=0.25` is **mandatory**, not cosmetic. Among machines with cheap
disk there are H100s at $4.26/h, and without a ceiling the autoscaler can
grab them.

### Before and after

| | Before | Now |
|---|---|---|
| Allocated disk | 60 GB | 18 GB |
| Disk cost | $12.00/month | ~$0.48/month |
| GPU | $0.190/h | $0.136-0.201/h |
| Total at 60 h/month | $23.40 | ~$12.90 |

> Machines with cheap disk tend to charge **more** per hour. Both cannot be
> optimized at once: with disk <=$2/month, network <$1/TB and <=$0.15/h
> simultaneously there are **zero offers** on the market. For an endpoint
> that is idle most of the time, prioritize disk. If you are going to put
> many hours on it, lower `dph_total` and accept paying more for disk.

### `verified=true`: removing it cost dearly

For a while this repo **removed** `verified=true` from the filter, because
with the budget of then it left a single offer:

| | Offers <=$0.15/h |
|---|---|
| with `verified=true` and `storage_cost<=0.11` | **1** ⚠️ |
| without `verified=true` | 7 (best at $0.109/h) |

The reasoning was correct in its mechanism — **with a single viable offer the
autoscaler relaxes the price ceiling** and rents above `dph_total`; that is
how a worker came in at $0.201/h with the ceiling at $0.15 — but the
conclusion treated the symptom. The damage was done by **running out of
offers**, not by verification.

**And removing `verified` had a much worse hidden cost: unusable workers.**
Measured on 2026-08-16 with two consecutive unverified machines (139268 and
147722): both advertised direct ports (`direct_port_count` 100 and 200) that
were actually **filtered**. The worker provisions fine, the pyworker starts,
the autoscaler marks it `idle` and routes to `https://<ip>:<port>`... but
that port *times out* from any external network, so **the pyworker receives
zero requests** (`num_requests_recieved: 0`) and every call dies by timeout.

The good lever is `storage_cost`, which costs cents:

| Configuration | Offers | Cheapest GPU | Disk ceiling at 18 GB |
|---|---|---|---|
| without `verified`, `storage<=0.11` | 7 | $0.107/h | $1.98/month |
| **`verified`, `storage<=0.20`** <- current | **11** | **$0.108/h** | $3.60/month |
| `verified`, `storage<=0.11` | **1** ⚠️ | $0.134/h | $1.98/month |

Loosening the disk recovers the range **and** the GPU price stays the same
($0.108 vs $0.107). You pay at most $1.62/month more of disk in exchange for
the machines actually working.

#### How to diagnose this quickly

The symptom is a request that hangs and dies with
`TimeoutError: Timed out after 61.4s waiting for worker`, with the worker in
`idle`. Checks, in order:

```powershell
# 1. which URL does the autoscaler deliver, and is that port open?
python -c "import socket;socket.create_connection(('<ip>',<port>),10)"

# 2. from outside your network, in case the block is yours
curl -s "https://check-host.net/check-tcp?host=<ip>:<port>&max_nodes=3"
```

Inside the worker, the pyworker speaks **HTTPS**, not HTTP: `curl -k
https://127.0.0.1:3000/health` returns **404** when healthy (that route does
not exist, but responding proves it is alive). With `http://` it gives
*Empty reply from server* and looks down without being down. And
`/workspace/pyworker.log` carries a `num_requests_recieved` that says whether
anything reaches it.

### Interruptible: not supported

Serverless workergroups **only use on-demand**. Tested on 2026-08-15:
setting `--launch_args "--bid_price 0.15"`, the created instance comes out
with `is_bid=False`. Interruptible instances (which come at half price) can
only be used outside serverless, with
`vastai create instance --bid_price`.

---

## Troubleshooting

### The worker cycles `model_loading -> error (timed out loading after 795s) -> reboot`

The classic symptom that **the service never starts**. It is almost always
the launch mode: if the template is in **Jupyter** (or ssh without calling
`entrypoint.sh`), Vast replaces the image's ENTRYPOINT with `/.launch`, which
only brings up sshd and jupyter. The supervisord of `vastai/comfy` never
runs, so there is no ComfyUI or pyworker and the autoscaler receives no
ready signal.

How to confirm it, by SSHing into the worker:

```bash
ps -p 1 -o cmd=            # if it says "bash /.launch" -> it is this
supervisorctl status       # "no such file" -> supervisord is not running
ss -lntp                   # only 8080 and 22; 18188 (ComfyUI) and 3000 (pyworker) missing
ls /var/log/supervisor /var/log/portal   # empty
```

And from outside: `image_runtype` in `vastai show instances --raw` says
`jupyter_direc ...` instead of `ssh`.

Fix: the template's `onstart` must call `entrypoint.sh &` and then
`start_server.sh`, as above.

**The other cause, and the one that really bites: the host has a bad
network.** Same script, same template, same image — and it comes out as an
infinite loop or a clean cycle depending on which machine you get. Measured
on 2026-08-16 with two consecutive instances:

| | bad host (`47857982`) | good host (`47861232`) |
|---|---|---|
| Real speed | ~350 KB/s | ~45 MB/s |
| `pip install` of Impact-Pack | ~15 min | seconds |
| R2 models (10 GB) | never got there | 3.7 min |
| Result | 3 timeouts -> `destroying` | `PROVISIONING_OK` in 14 min |

The autoscaler has a **fixed loading timeout of 791 s** (~13.2 min) that does
not appear in the endpoint config and is not configurable from there. When it
runs out it does `restart_instance` (not `destroy`), so the container
restarts and the provisioning **starts over from zero**. Three consecutive
failures and it does destroy the machine. At 350 KB/s the pip alone eats
15 min: infinite loop guaranteed.

How to tell "hung" from "slow", from the worker:

```bash
ps -eo pid,ppid,etime,stat,args --forest       # is there a live pip child?
ls -l /proc/<pid>/fd                           # which .whl is downloading?
stat -c %s <whl>; sleep 20; stat -c %s <whl>   # real speed
cat /proc/net/dev                              # total instance bytes
```

The log looks frozen because `PIP_ARGS` carried `-q`: in silent mode pip
writes nothing for minutes even while progressing. Now it uses
`--progress-bar off` and `watch_progress` reports to the Discord channel
every 2 min.

**What does help** (applied): persistent pip cache at
`$WORKSPACE_DIR/.cache/pip` via `PIP_CACHE_DIR`, instead of
`--no-cache-dir`. Since `restart_instance` keeps `/workspace`, the 2nd
attempt reuses the already-downloaded wheels and fits well inside the
timeout. That gives three opportunities (~45 min) to converge before the
autoscaler destroys the machine.

**What does NOT help, checked:** moving the wheels to R2. R2 goes at 361 KB/s
from the bad worker — *exactly as slow as PyPI* (320 KB/s), and with 4
parallel connections it adds up to ~730 KB/s (~2x, not 4x). The bottleneck is
the instance's bandwidth, not the origin. The workergroup filter does not fix
it either: `search_query` filters by `inet_down_cost<=0.005` (network price,
not speed) and the pool's 10 offers already advertise `inet_down` between 190
and 1368 Mbit/s. The bad host advertised **1376 Mbit/s** and delivered 3. The
data is self-reported and no filter saves you.

### `HF_TOKEN must be set when BACKEND is set!`

Validation of the pyworker's `start_server.sh`. It requires `HF_TOKEN` to
exist if `BACKEND` is defined, even if you download nothing from HuggingFace.
Put it in the template env.

### The worker shows as `stopped`

Normal, not a failure. With `min_load=0` the autoscaler powers down workers
when idle and leaves them cold. The next request wakes it up.

**And it is very cheap to leave it like that.** A stopped instance only pays
the disk:

| State | Cost | Per month |
|---|---|---|
| Powered on | `dph_total` 0.125 $/h | ~$90 |
| Stopped (disk only) | `storage_total_cost` 0.005 $/h | ~$3.60 |

25x cheaper, and it keeps the whole `/workspace`: the models in
`/workspace/ComfyUI/models` and the pip cache. A start from stopped skips the
~22 GB download and the pip minutes. That is exactly what the autoscaler does
with `cold_workers=1`; the problem is that to keep a stopped machine it first
had to validate it once — it is the reward for the provisioning finishing
well, not an alternative to fixing it.

Caveats: a stopped instance **does not reserve the GPU**. The host can rent
that hardware to someone else and then it will not start when you want to
power it on. It is cheap precisely because it guarantees nothing.

### Discord notifications

The provisioning reports to the channel via webhook: start, each milestone of
the 5 phases, progress every 2 min (pip cache size, model size, free disk and
the last live log line), `PROVISIONING_OK`, failure with the log block, and a
sentinel that stays alive afterwards to warn of power-on, outage and
shutdown.

Configured with `DISCORD_WEBHOOK` in the `.env`. **It travels via the
template env**, not inside the `.sh`: that file is served from a public R2
URL and anyone could read it. `renew_provisioning.py` puts it in the
`template_env` when publishing; if it is not defined, the provisioning does
not notify and works the same (the whole block is a no-op).

Two details that cost blood:

- **Discord returns `403 Forbidden` to urllib's default User-Agent**
  (`Python-urllib/3.x`). You must send your own header:
  `headers={"User-Agent": "mizuki-provision/1.0"}` -> `204`. Same pattern as
  Cloudflare/R2, where `curl/8.0` is used.
- **The webhook must never be able to take down the provisioning.** The
  script runs with `set -euo pipefail`; all emitters end in `|| true` and
  with a short timeout, tested with a broken webhook and with an empty one
  (`exit 0` in both cases).

### `invalid template hash or id`

`vastai update template` **changes the `hash_id`**. After touching the
template the workergroup must be re-pointed to the new hash:

```powershell
vastai search templates "creator_id=$VAST_CREATOR_ID"    # get the current hash
vastai update workergroup $VAST_WORKERGROUP_ID --endpoint_id $VAST_ENDPOINT_ID --template_hash <HASH> --template_id $VAST_TEMPLATE_ID
```

### Black images

Clip skip. Node `60` (`CLIPSetLastLayer`) must be at `-2`. With `-1`,
`waiIllustriousSDXL_v170` produces black images.

### A model or custom node is missing

The request fails in ComfyUI with the name of the node or file. Add it to
`serverless_provision.sh` (the `WANTED` list for models, `install_node` for
nodes), upload it to R2 and run
`vastai update workers $VAST_WORKERGROUP_ID`.

---

## Pending maintenance

- [x] ~~`PROVISIONING_SCRIPT` was a presigned URL expiring after 7 days.~~
      Solved: the bucket serves via *Public Development URL* and the URL is
      permanent. `renew_provisioning.py` remains the publish command (upload
      to R2, verify content and re-point template + workergroup), but there
      is nothing left to renew on a calendar.
- [ ] **Parallelize the provisioning.** Today the 5 phases run in series.
      Phase 3 (custom nodes: PyPI + GitHub) and phase 4 (R2 models) do not
      depend on each other and use different resources; overlapping them
      takes the shorter one off the critical path. The `URL_FILES` loop
      (loose HuggingFace weights) is one `curl` after another and
      parallelizes in four lines. Real ceiling measured: **~2x**, not 4x —
      the bottleneck is the instance's bandwidth, not the number of streams.
      **Do not parallelize two `pip install`s on the same venv**: they step
      on each other in `site-packages` and give intermittently corrupted
      environments. And the `trap on_err` with background processes needs
      explicit `wait`, or failures stop reporting.
- [ ] **`--bbox` not implemented.** The node is already identified
      (`AILab_CropObject`, see *Character resolution*); what is missing is
      wiring it between BiRefNet and the scaling into the box.
- [ ] **`HF_TOKEN=hf_placeholder_not_used`** in the template. Replace with a
      real one if downloads from HuggingFace are ever added.
- [ ] The URLs of generated images expire after 7 days. If they must be kept,
      download them or move them inside R2.
