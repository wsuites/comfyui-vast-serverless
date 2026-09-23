#!/bin/bash
# =============================================================================
# serverless_provision.sh - provisioning for the "mizuki" serverless endpoint
#
# Runs via PROVISIONING_SCRIPT (the vastai/comfy image provisioner downloads
# it and runs it BEFORE marking the worker ready). If this script exits with
# code != 0, the worker is NOT marked ready. That is intentional.
#
# Requires in the environment (comes from the template / Vast account env vars):
#   S3_ACCESS_KEY_ID  S3_SECRET_ACCESS_KEY  S3_BUCKET_NAME  S3_ENDPOINT_URL
#   S3_REGION (optional, default "auto")
#
# Log: $MODEL_LOG (default /var/log/portal/comfyui.log)
# =============================================================================
set -euo pipefail

# =============================================================================
# CONFIGURATION - this is the only thing you normally need to touch
# =============================================================================

# --- FEATURES ----------------------------------------------------------------
# true/false per feature. Each one drags its models, its custom nodes and its
# pip packages; turning off what you do not use saves disk and cold start time.
# The client flag that enables each one is in the comment.
#
#   IMPORTANT: turning one off here does NOT change the workflows. If you send
#   a workflow that uses something that is off, ComfyUI fails with the name of
#   the model or the node.
FEAT_UPSCALE=true          # UltimateSDUpscale of wf.json            67 MB
FEAT_RMBG=true             # BiRefNet, cuts out the character        ~1 GB
FEAT_CONTROLNET=true       # ControlNet union, required by any --pose  2.5 GB
FEAT_POSE_DWPOSE=true      # --pose dwpose                          351 MB
FEAT_POSE_OPENPOSE=false   # --pose openpose: FAILS with anime      ~430 MB
FEAT_FACE=true             # --face (FaceDetailer)                   52 MB
FEAT_HANDS_YOLO=true       # --hands yolo                            22 MB
FEAT_HANDS_MESH=true       # --hands mesh (MeshGraphormer)         1.37 GB
FEAT_ANIMA=true            # Anima (ab_modelo.py A/B)               ~5.6 GB

on() { [ "${1:-false}" = "true" ]; }

# --- Models from R2.  "<key in the bucket>|<relative path inside models/>" ----
#
# TWO LISTS, and the split is what the cold-start budget rests on. MODELS is
# what the endpoint cannot answer a single request without; MODELS_DEFERRED is
# everything gated behind a feature flag, which no request uses until it asks
# for that feature by name.
#
# Measured 2026-09-22 (run7, machine 151649, a healthy host): 23s renting, 193s
# pulling the 14 GB image, 322s on ~22 GB of models - 564s to ready, against a
# 600s budget. Both halves ran at ~70 MB/s, so nothing was stalling: the boot
# was simply carrying 36 GB before answering anything. A host any slower than
# that one misses the budget, and most of them are slower.
#
# Deferring the feature models takes the blocking set from ~22 GB to ~7.3 GB.
# They keep downloading right after the endpoint reports ready, so the window
# where a --pose or an anima request would find its file missing is the couple
# of minutes after a cold start, and the node's own mid-request download is
# still there as the floor under it.
MODELS=(
  "comfy-stack/models/checkpoints/waiIllustriousSDXL_v170.safetensors|checkpoints/waiIllustriousSDXL_v170.safetensors"
  "comfy-stack/models/loras/stuffy_ai_style_ilxl_v2_goofy.safetensors|loras/stuffy_ai_style_ilxl_v2_goofy.safetensors"
)
MODELS_DEFERRED=()
if on "$FEAT_FACE"; then
  # 52 MB and on the default request path: not worth deferring.
  MODELS+=("comfy-stack/models/ultralytics/bbox/face_yolov8m.pt|ultralytics/bbox/face_yolov8m.pt")
fi
if on "$FEAT_UPSCALE"; then
  MODELS+=("comfy-stack/models/upscale_models/4x_NMKD-Siax_200k.pth|upscale_models/4x_NMKD-Siax_200k.pth")
fi
if on "$FEAT_RMBG"; then
  # Mirrored on R2 to not depend on HuggingFace, which otherwise would
  # download them on the first request. The .py and config.json files are
  # mandatory: without them it does not load.
  MODELS_DEFERRED+=(
    "comfy-stack/models/RMBG/BiRefNet/BiRefNet-general.safetensors|RMBG/BiRefNet/BiRefNet-general.safetensors"
    "comfy-stack/models/RMBG/BiRefNet/BiRefNet_config.py|RMBG/BiRefNet/BiRefNet_config.py"
    "comfy-stack/models/RMBG/BiRefNet/birefnet.py|RMBG/BiRefNet/birefnet.py"
    "comfy-stack/models/RMBG/BiRefNet/birefnet_lite.py|RMBG/BiRefNet/birefnet_lite.py"
    "comfy-stack/models/RMBG/BiRefNet/config.json|RMBG/BiRefNet/config.json"
  )
fi
if on "$FEAT_CONTROLNET"; then
  # Union: openpose, depth, canny... all in one model. The 'mesh' hands pass
  # uses it in depth mode, so it also needs it.
  MODELS_DEFERRED+=("comfy-stack/models/controlnet/controlnet-union-sdxl-1.0.safetensors|controlnet/controlnet-union-sdxl-1.0.safetensors")
fi
if on "$FEAT_ANIMA"; then
  # Anima is NOT an SDXL checkpoint: it is a 2B DiT (finetune of
  # nvidia/Cosmos-Predict2-2B-Text2Image) with a Qwen3-0.6B text encoder and
  # the Qwen-Image VAE. There is no CheckpointLoaderSimple path -- it needs
  # three separate files in three different folders, which is why they are
  # listed apart from the checkpoints above.
  #
  # DISK: ~5.6 GB on top of everything else. With VAST_DISK_SPACE=18 and the
  # full SDXL feature set already installed there is not room; raise the disk
  # or turn off the features the A/B does not use (it is bare text2img: it
  # needs no controlnet, no rmbg, no pose, no face, no hands, no upscale).
  #
  # Mirror these to R2 first, same as BiRefNet, so provisioning does not
  # depend on HuggingFace. Source: circlestone-labs/Anima, split_files/.
  MODELS_DEFERRED+=(
    "comfy-stack/models/diffusion_models/anima-aesthetic-v1.0.safetensors|diffusion_models/anima-aesthetic-v1.0.safetensors"
    "comfy-stack/models/text_encoders/qwen_3_06b_base.safetensors|text_encoders/qwen_3_06b_base.safetensors"
    "comfy-stack/models/vae/qwen_image_vae.safetensors|vae/qwen_image_vae.safetensors"
  )
fi

# --- R2 files that do NOT go under models/. Path relative to COMFY_DIR. -------
EXTRA_FILES=()
if on "$FEAT_POSE_OPENPOSE"; then
  EXTRA_FILES+=(
    "comfy-stack/custom_nodes/comfyui_controlnet_aux/ckpts/lllyasviel/Annotators/body_pose_model.pth|custom_nodes/comfyui_controlnet_aux/ckpts/lllyasviel/Annotators/body_pose_model.pth"
    "comfy-stack/custom_nodes/comfyui_controlnet_aux/ckpts/lllyasviel/Annotators/hand_pose_model.pth|custom_nodes/comfyui_controlnet_aux/ckpts/lllyasviel/Annotators/hand_pose_model.pth"
    "comfy-stack/custom_nodes/comfyui_controlnet_aux/ckpts/lllyasviel/Annotators/facenet.pth|custom_nodes/comfyui_controlnet_aux/ckpts/lllyasviel/Annotators/facenet.pth"
  )
fi

# --- Downloads by direct URL.  "<url>|<path relative to COMFY_DIR>" ----------
# For public HuggingFace weights not worth mirroring on R2. Unlike
# MODELS/EXTRA_FILES the size is not checked against a source, it just skips
# if the file already exists.
AUX_CKPTS="custom_nodes/comfyui_controlnet_aux/ckpts"
URL_FILES=()
if on "$FEAT_POSE_DWPOSE"; then
  # Without this the node downloads them from HuggingFace MID-REQUEST
  # (measured: the worker had them without being in the provisioning).
  URL_FILES+=(
    "https://huggingface.co/yzd-v/DWPose/resolve/main/yolox_l.onnx|$AUX_CKPTS/yzd-v/DWPose/yolox_l.onnx"
    "https://huggingface.co/yzd-v/DWPose/resolve/main/dw-ll_ucoco_384.onnx|$AUX_CKPTS/yzd-v/DWPose/dw-ll_ucoco_384.onnx"
  )
fi
if on "$FEAT_HANDS_YOLO"; then
  URL_FILES+=("https://huggingface.co/Bingsu/adetailer/resolve/main/hand_yolov8s.pt|models/ultralytics/bbox/hand_yolov8s.pt")
fi
if on "$FEAT_HANDS_MESH"; then
  # The third file of that repo (control_sd15_inpaint_depth_hand, 722MB) is NOT
  # needed: it is the SD1.5 ControlNet and here the SDXL union is used in depth.
  URL_FILES+=(
    "https://huggingface.co/hr16/ControlNet-HandRefiner-pruned/resolve/main/graphormer_hand_state_dict.bin|$AUX_CKPTS/hr16/ControlNet-HandRefiner-pruned/graphormer_hand_state_dict.bin"
    "https://huggingface.co/hr16/ControlNet-HandRefiner-pruned/resolve/main/hrnetv2_w64_imagenet_pretrained.pth|$AUX_CKPTS/hr16/ControlNet-HandRefiner-pruned/hrnetv2_w64_imagenet_pretrained.pth"
  )
fi

# --- Custom nodes.  "<git repo>|<directory>|<recursive>|<norequirements>" ----
#   recursive       -> clone with submodules
#   norequirements  -> do NOT install its requirements.txt; deps go in PIP_EXTRA
NODES=()
if on "$FEAT_FACE" || on "$FEAT_HANDS_YOLO"; then
  # FaceDetailer and UltralyticsDetectorProvider. FaceDetailer is NOT
  # face-specific: with a hands bbox_detector it does the hands pass.
  NODES+=(
    "https://github.com/ltdrdata/ComfyUI-Impact-Pack.git|ComfyUI-Impact-Pack||"
    "https://github.com/ltdrdata/ComfyUI-Impact-Subpack.git|ComfyUI-Impact-Subpack||"
  )
fi
if on "$FEAT_UPSCALE"; then
  NODES+=("https://github.com/ssitu/ComfyUI_UltimateSDUpscale.git|ComfyUI_UltimateSDUpscale|recursive|")
fi
if on "$FEAT_RMBG"; then
  # RMBG's requirements.txt drags onnxruntime-gpu, groundingdino-py, decord and
  # SAM2/SAM3: ~2GB that we do not use. We only want BiRefNet.
  NODES+=("https://github.com/1038lab/ComfyUI-RMBG.git|ComfyUI-RMBG||norequirements")
fi
if on "$FEAT_POSE_DWPOSE" || on "$FEAT_POSE_OPENPOSE" || on "$FEAT_HANDS_MESH"; then
  # pose/depth annotators for ControlNet; MeshGraphormer lives inside here
  NODES+=("https://github.com/Fannovel16/comfyui_controlnet_aux.git|comfyui_controlnet_aux||")
fi

# --- Extra pip packages needed by the nodes above ----------------------------
PIP_EXTRA=(opencv-python-headless scipy scikit-image spandrel boto3 huggingface-hub
           transformers)
if on "$FEAT_FACE" || on "$FEAT_HANDS_YOLO"; then
  PIP_EXTRA+=(ultralytics)
fi
if on "$FEAT_RMBG"; then
  PIP_EXTRA+=(transparent-background)
fi
if on "$FEAT_HANDS_MESH"; then
  # Without these, the node installs them via pip MID-REQUEST.
  PIP_EXTRA+=(mediapipe trimesh)
fi

# Checkpoint used by the benchmark workflow (it has to be in MODELS)
BENCHMARK_CKPT="waiIllustriousSDXL_v170.safetensors"

# =============================================================================
# From here on you usually do not need to touch anything
# =============================================================================

WORKSPACE_DIR="${WORKSPACE:-/workspace}"
MODEL_LOG="${MODEL_LOG:-/var/log/portal/comfyui.log}"
mkdir -p "$(dirname "$MODEL_LOG")"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] provision: $1" | tee -a "$MODEL_LOG"; }

# --- Discord notifications ----------------------------------------------------
# The webhook is NOT written here: this file is published on a public R2 URL.
# It arrives via the template environment (-e DISCORD_WEBHOOK=...). If it is
# not set, everything below is a no-op and provisioning keeps going.
DISCORD_WEBHOOK="${DISCORD_WEBHOOK:-}"
T0=$(date +%s)
WHO="${CONTAINER_ID:-${VAST_CONTAINERLABEL:-$(hostname)}}"
GPU=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
IP=$(curl -s -m 5 https://api.ipify.org 2>/dev/null || echo "?")

elapsed() { printf '%dm%02ds' $(( ($(date +%s) - T0) / 60 )) $(( ($(date +%s) - T0) % 60 )); }

# sends a message. Hardened: bad network, 429 or deleted webhook can never
# take down the provisioning (hence the || true and the short timeout).
dc() {
    [ -n "$DISCORD_WEBHOOK" ] || return 0
    python3 - "$DISCORD_WEBHOOK" "$1" <<'PY' >/dev/null 2>&1 || true
import json, sys, urllib.request
url, content = sys.argv[1], sys.argv[2][:1900]
body = json.dumps({"content": content, "username": "mizuki-provision",
                   "allowed_mentions": {"parse": []}}).encode()
try:
    urllib.request.urlopen(urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json",
                                 # without a proper User-Agent Discord returns 403
                                 "User-Agent": "mizuki-provision/1.0"}), timeout=10).read()
except Exception:
    pass
PY
}

# milestone = to the worker log AND to the channel
hito() { log "$1"; dc "[$WHO $(elapsed)] $1"; }

# dumps the last log lines inside a code block
dc_log() {
    [ -n "$DISCORD_WEBHOOK" ] || return 0
    local n="${1:-40}" cola
    cola=$(tail -n "$n" "$MODEL_LOG" 2>/dev/null | tail -c 1500)
    dc "\`\`\`$(printf '%s' "$cola")\`\`\`"
}

on_err() {
    log "[ERROR] failure on line $1 (exit $2) - the worker will NOT be marked ready"
    dc ":x: **provisioning FAILED** \`$WHO\` line $1 (exit $2) after $(elapsed) - the worker is not marked ready"
    dc_log 40
    ERR_YA_AVISADO=1
}
trap 'on_err $LINENO $?' ERR

# --- progress poll ------------------------------------------------------------
# pip and the R2 downloads go minutes without writing anything; without this
# the channel (and the log) look stuck when there is actually work. Reports
# every 2 min the size of the pip cache, of models/ and the last live log line.
watch_progress() {
    local prev="" now
    while sleep 120; do
        now="pip:$(du -sm "${PIP_CACHE_DIR:-/tmp}" 2>/dev/null | cut -f1)MB"
        now="$now models:$(du -sm "${MODELS_DIR:-/tmp}" 2>/dev/null | cut -f1)MB"
        now="$now free:$(df -h "$WORKSPACE_DIR" 2>/dev/null | awk 'NR==2{print $4}')"
        if [ "$now" = "$prev" ]; then
            dc ":hourglass: [$WHO $(elapsed)] $now _(no change)_ · $(tail -n 1 "$MODEL_LOG" 2>/dev/null | tail -c 200)"
        else
            dc ":arrow_forward: [$WHO $(elapsed)] $now · $(tail -n 1 "$MODEL_LOG" 2>/dev/null | tail -c 200)"
        fi
        prev="$now"
    done
}

dc ":rocket: **provisioning STARTS** \`$WHO\` · ${GPU:-GPU?} · IP $IP"
log "=== start ==="

# The explicit 'exit 1' (credentials, ComfyUI not found, pip) do NOT trigger
# the ERR trap, only the EXIT. Here that gap is closed: any exit != 0 that
# does not already come from on_err is reported with its log tail.
ERR_YA_AVISADO=0
# NOT started here: watch_progress measures PIP_CACHE_DIR and MODELS_DIR, and a
# background subshell keeps the variable values as of fork time. Started at
# this point both were still unset, so it measured /tmp and reported a flat
# "pip:1MB models:1MB" while gigabytes were downloading. It now starts below,
# once both paths exist.
WATCH_PID=""
on_exit() {
    local st=$?
    [ -n "$WATCH_PID" ] && kill "$WATCH_PID" 2>/dev/null || true
    if [ "$st" != 0 ] && [ "$ERR_YA_AVISADO" = 0 ]; then
        dc ":x: **provisioning ABORTED** \`$WHO\` (exit $st) after $(elapsed)"
        dc_log 40
    fi
}
trap on_exit EXIT

# --- image venv ---------------------------------------------------------------
if [ -f /venv/main/bin/activate ]; then
    # shellcheck source=/dev/null
    . /venv/main/bin/activate
    log "venv /venv/main activated"
fi
# pip cache on persistent disk. The autoscaler kills the load at ~791s and
# does restart_instance; with --no-cache-dir every restart re-downloaded the
# same wheels from scratch (142 MB direct, ~15 min on a 350 KB/s host) and it
# never fit in the timeout: infinite loop. With a cache, the 2nd attempt
# reuses what was already downloaded and converges even if the host network
# is bad.
export PIP_CACHE_DIR="$WORKSPACE_DIR/.cache/pip"
mkdir -p "$PIP_CACHE_DIR"
# no -q: in silent mode pip writes nothing for minutes and the log looks
# stuck when it is actually progressing.
PIP_ARGS="--cache-dir $PIP_CACHE_DIR --progress-bar off"
[ -n "${VIRTUAL_ENV:-}" ] || PIP_ARGS="$PIP_ARGS --break-system-packages"

# --- [1/5] locate ComfyUI ----------------------------------------------------
COMFY_DIR=""
for d in "$WORKSPACE_DIR/ComfyUI" /opt/workspace-internal/ComfyUI /opt/ComfyUI /root/ComfyUI; do
    if [ -d "$d" ]; then COMFY_DIR="$d"; break; fi
done
if [ -z "$COMFY_DIR" ]; then
    log "[ERROR] ComfyUI directory not found"
    exit 1
fi
MODELS_DIR="$COMFY_DIR/models"
NODES_DIR="$COMFY_DIR/custom_nodes"
mkdir -p "$MODELS_DIR"/{checkpoints,loras,upscale_models} "$MODELS_DIR/ultralytics/bbox" "$NODES_DIR"
hito "[1/5] COMFY_DIR=$COMFY_DIR"

# --- the card has to be ours -------------------------------------------------
# Vast rents a GPU, not a machine, and nothing stops a host from packing other
# tenants onto the same card. Measured 2026-09-22 on machine 146299: seven
# compute processes, 23568 of 24564 MiB taken, 494 MiB free. Everything still
# looked healthy from outside - the 512x512 benchmark fits in the scraps and
# reports a normal score, so PROVISIONING_OK was emitted and the first real
# 1024 render fell back to tiled VAE and then timed out.
#
# The fraction, not an absolute floor: this endpoint rents 16 GB cards as well
# as 24 GB ones, and a clean 16 GB card has less free than a crowded 24 GB one.
MIN_FREE_VRAM_FRACTION="${MIN_FREE_VRAM_FRACTION:-0.85}"
if command -v nvidia-smi >/dev/null 2>&1; then
    vram_line=$(nvidia-smi --query-gpu=memory.total,memory.free \
                           --format=csv,noheader,nounits 2>/dev/null | head -1)
    vram_total=$(echo "$vram_line" | cut -d, -f1 | tr -d ' ')
    vram_free=$(echo "$vram_line" | cut -d, -f2 | tr -d ' ')
    if [ -n "$vram_total" ] && [ "$vram_total" -gt 0 ] 2>/dev/null; then
        # integer math: free * 100 / total, compared against the fraction * 100
        pct_free=$(( vram_free * 100 / vram_total ))
        min_pct=$(awk -v f="$MIN_FREE_VRAM_FRACTION" 'BEGIN{printf "%d", f*100}')
        if [ "$pct_free" -lt "$min_pct" ]; then
            log "[ERROR] GPU already in use by another tenant:"
            log "[ERROR]   ${vram_free} of ${vram_total} MiB free (${pct_free}%,"
            log "[ERROR]   need ${min_pct}%). Processes on the card:"
            nvidia-smi --query-compute-apps=pid,used_memory \
                       --format=csv 2>&1 | sed 's/^/[ERROR]   /' | tee -a "$MODEL_LOG"
            log "[ERROR] Refusing to provision: models would download onto a card"
            log "[ERROR] that cannot render. Replace this machine."
            dc ":no_entry: **GPU OVERSUBSCRIBED** \`$WHO\` - ${vram_free}/${vram_total} MiB free. Replacing."
            exit 1
        fi
        hito "[1/5] GPU clear: ${vram_free}/${vram_total} MiB free (${pct_free}%)"
    fi
fi

# now that PIP_CACHE_DIR and MODELS_DIR are both set, the watcher measures the
# right directories (see the note where WATCH_PID is declared)
watch_progress & WATCH_PID=$!

# --- [2/5] validate S3 credentials before anything ------------------------------
missing=""
for v in S3_ACCESS_KEY_ID S3_SECRET_ACCESS_KEY S3_BUCKET_NAME S3_ENDPOINT_URL; do
    [ -n "${!v:-}" ] || missing="$missing $v"
done
if [ -n "$missing" ]; then
    log "[ERROR] missing S3 environment variables:$missing"
    exit 1
fi
hito "[2/5] S3 credentials present (bucket=$S3_BUCKET_NAME)"

# --- [3/5] custom nodes: clone in PARALLEL ------------------------------------
# Only the clone runs here. The pip requirements are deferred to a single
# combined pass further down, so that cloning, pip and the model downloads all
# overlap instead of running strictly one after the other. On a cold start
# that turns ~5 serial network waits into ~1 wall-clock wait.
CLONE_PIDS=()
CLONE_NAMES=()
clone_node() {
    local repo="$1" name="$2" recursive="${3:-}"
    if [ -d "$NODES_DIR/$name" ]; then
        log "node already present: $name"
        return 0
    fi
    log "cloning $name"
    if [ -n "$recursive" ]; then
        git clone --depth 1 --recursive "$repo" "$NODES_DIR/$name"
    else
        git clone --depth 1 "$repo" "$NODES_DIR/$name"
    fi
}

for entry in "${NODES[@]:-}"; do
    [ -n "$entry" ] || continue          # the array may end up empty due to a toggle
    IFS='|' read -r repo name recursive norequirements <<< "$entry"
    clone_node "$repo" "$name" "$recursive" &
    CLONE_PIDS+=($!)
    CLONE_NAMES+=("$name")
done

# --- [4/5] models from R2 ----------------------------------------------------
# Launched in the BACKGROUND here, before waiting for the clones or pip: the
# three network-heavy phases (git clones, pip, model downloads) now overlap
# instead of running strictly one after the other. The wait happens further
# down, right before the benchmark step needs the files.
#
# DEPENDENCY: the downloader below imports boto3, and boto3 is normally
# installed by the combined pip pass that now runs AFTER this fork. That was a
# real failure: "ModuleNotFoundError: No module named 'boto3'", provisioning
# aborted at 1m15s. So boto3 is installed on its own first (it is in
# PIP_CACHE_DIR after the first boot, so this is seconds).
# shellcheck disable=SC2086
pip install $PIP_ARGS boto3 \
    || { log "[ERROR] could not install boto3 (needed to fetch models)"; exit 1; }
export COMFY_MODELS_DIR="$MODELS_DIR"
# A "key|ABSOLUTE destination" per line is passed to python. MODELS is relative
# to models/ and EXTRA_FILES relative to COMFY_DIR, but here both are resolved.
COMFY_MODEL_LIST=$(
    # Beware of the 'if': a '[ -n "$k" ] && echo' that fails on the LAST
    # iteration makes the whole substitution exit with code 1 and, with set -e,
    # kills the provisioning. It happens as soon as an array is left empty by a
    # toggle set to false.
    for e in "${MODELS[@]:-}"; do
        IFS='|' read -r k r <<< "$e"
        if [ -n "$k" ]; then echo "$k|$MODELS_DIR/$r"; fi
    done
    for e in "${EXTRA_FILES[@]:-}"; do
        IFS='|' read -r k r <<< "$e"
        if [ -n "$k" ]; then echo "$k|$COMFY_DIR/$r"; fi
    done
)
COMFY_MODEL_LIST_DEFERRED=$(
    for e in "${MODELS_DEFERRED[@]:-}"; do
        IFS='|' read -r k r <<< "$e"
        if [ -n "$k" ]; then echo "$k|$MODELS_DIR/$r"; fi
    done
)
export COMFY_MODEL_LIST COMFY_MODEL_LIST_DEFERRED
# Written the moment the blocking set is on disk. The benchmark waits for this
# file, not for the downloader to exit, so the deferred half keeps running past
# the point where the endpoint starts serving.
MODELS_CRITICAL_MARK="$COMFY_DIR/.critical_models_ok"
rm -f "$MODELS_CRITICAL_MARK"

# Same downloader for both passes; the list comes in through the environment.
fetch_models() {
    COMFY_MODEL_LIST="$1" python3 - <<'PY'
import os, sys, subprocess, concurrent.futures as cf
from pathlib import Path
import boto3

models_dir = Path(os.environ["COMFY_MODELS_DIR"])
bucket = os.environ["S3_BUCKET_NAME"]

# R2 key -> relative path inside models/ (comes from the MODELS array of bash)
WANTED = {}
for line in os.environ["COMFY_MODEL_LIST"].splitlines():
    line = line.strip()
    if not line:
        continue
    key, _, rel = line.partition("|")
    WANTED[key.strip()] = rel.strip()
if not WANTED:
    print("no models declared", file=sys.stderr)
    sys.exit(1)

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["S3_ENDPOINT_URL"],
    aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
    region_name=os.environ.get("S3_REGION", "auto"),
)
# Presigned GET against the S3 endpoint, NOT the public r2.dev URL.
# Measured 2026-09-22 on worker 52056650 (machine 148725), same host,
# same object, same moment: pub-*.r2.dev returned 28 KB/s on the
# 500MB-510MB range and 150 KB/s on 0-10MB, while the presigned URL from
# 20ee...r2.cloudflarestorage.com returned 9.6 MB/s and 12.4 MB/s. The
# public development domain is throttled for that worker's IP; the S3
# endpoint is not.
#
# --speed-limit/--speed-time turn a silent stall into a retry: without them
# a connection that drops to 0 B/s hangs until the whole fetch (and the
# provisioning) times out, which is exactly how the anima object aborted 3
# attempts in a row at ~512 MB. With them curl aborts the stalled connection
# after 30s below 10 KB/s, and -C - resumes from the byte it got to.
#
# FALLBACKS: same bytes on a different origin, used when the R2 fetch does
# not complete. Useful because the failure is route-specific: the wai 6.9 GB
# object downloaded fine on the same host that stalled on anima.
CURL = ["curl", "-4", "-fsSL", "--retry", "2", "--retry-delay", "3",
        "--retry-all-errors", "--connect-timeout", "15",
        "--speed-limit", "30720", "--speed-time", "25", "-C", "-"]

FALLBACKS = {
    "comfy-stack/models/diffusion_models/anima-aesthetic-v1.0.safetensors":
        "https://huggingface.co/circlestone-labs/Anima/resolve/main/"
        "split_files/diffusion_models/anima-aesthetic-v1.0.safetensors",
    "comfy-stack/models/text_encoders/qwen_3_06b_base.safetensors":
        "https://huggingface.co/circlestone-labs/Anima/resolve/main/"
        "split_files/text_encoders/qwen_3_06b_base.safetensors",
    "comfy-stack/models/vae/qwen_image_vae.safetensors":
        "https://huggingface.co/circlestone-labs/Anima/resolve/main/"
        "split_files/vae/qwen_image_vae.safetensors",
}

PROBE = ["curl", "-4", "-fsSL", "-o", "/dev/null", "--max-time", "8",
         "-w", "%{speed_download}"]


def probe_speed(url, offset=0):
    """Bytes/s over a 2 MB ranged GET starting at ``offset``. 0.0 on failure.

    Probing AT THE RESUME POINT matters: measured 2026-09-22, the same R2 url
    served 12 MB/s at offset 0 and 5 KB/s at 600 MB on the anima object, so a
    probe at 0 would pick exactly the route that stalls later.
    Measured 2026-09-22: R2 and HuggingFace can differ by 3 orders of
    magnitude for the SAME worker, and the slow one can still trickle bytes
    forever (5 KB/s on anima). Probing first and downloading from whichever
    answers faster avoids spending --retry 8 on a dead route, which is how the
    previous version still stalled for 11 minutes.
    """
    rng = f"{offset}-{offset + 2_000_000}"
    try:
        p = subprocess.run([*PROBE, "-r", rng, url],
                           capture_output=True, text=True, timeout=20)
        return float(p.stdout.strip() or 0)
    except Exception:
        return 0.0

def fetch(item):
    key, rel = item
    dst = Path(rel)          # bash already resolves the absolute path
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        remote = s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
    except Exception as e:
        return f"MISSING on R2: {key} ({e})"
    if dst.exists() and dst.stat().st_size == remote:
        return f"ok (cache) {rel}"
    tmp = dst.with_suffix(dst.suffix + ".part")
    # The old fetcher (boto3 download_file) left random-suffixed leftovers
    # (e.g. .part.9a90397C). Reclaim the largest one as the resume point so a
    # half-downloaded object is not thrown away, and drop the rest.
    leftovers = sorted(dst.parent.glob(dst.name + ".part.*"),
                       key=lambda p: p.stat().st_size, reverse=True)
    if leftovers:
        if not tmp.exists() or leftovers[0].stat().st_size > tmp.stat().st_size:
            leftovers[0].replace(tmp)
        for p in leftovers[1:]:
            p.unlink(missing_ok=True)
    if tmp.exists() and tmp.stat().st_size == remote:
        tmp.rename(dst)
        return f"ok (resumed complete) {rel}"
    if tmp.exists() and tmp.stat().st_size > remote:
        tmp.unlink()             # corrupt: larger than the object itself
    # Presigned GET against the S3 endpoint, NOT the public r2.dev URL.
    # Measured 2026-09-22 on worker 52056650 (machine 148725), same host,
    # same object, same moment: pub-*.r2.dev returned 28 KB/s on the
    # 500MB-510MB range and 150 KB/s on 0-10MB, while the presigned URL from
    # 20ee...r2.cloudflarestorage.com returned 9.6 MB/s and 12.4 MB/s. The
    # public development domain is throttled for that worker's IP; the S3
    # endpoint is not. That throttling is what stalled the anima object at
    # ~512 MB on every attempt and aborted provisioning 3 times.
    url = s3.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=7*24*3600)
    candidates = [(url, "r2")]
    alt = FALLBACKS.get(key)
    if alt:
        candidates.append((alt, "hf"))
    # Probe every origin and download from the fastest. Only worth the extra
    # 2 MB GET when there is a real choice; with one origin go straight in.
    if len(candidates) > 1:
        resume = tmp.stat().st_size if tmp.exists() else 0
        scored = sorted(((probe_speed(u, resume), u, o) for u, o in candidates),
                        reverse=True)
        candidates = [(u, o) for _, u, o in scored]
        print("    probe " + " ".join(
            f"{o}={s/1e6:.1f}MB/s" for s, _, o in scored), flush=True)
        # A source that cannot even answer the probe is not worth a long fetch;
        # keep it only as the last resort so a transient probe failure does not
        # delete the only viable origin.
        if scored[0][0] <= 0:
            candidates = [(u, o) for _, u, o in scored]
    attempt = ""
    size = 0
    for cand, origin in candidates:
        # curl -C - resumes from tmp's current size on whichever origin is
        # used, so switching origin keeps what the previous one already got:
        # the bytes are the same object (R2 mirrors the HF file).
        proc = subprocess.run([*CURL, "-o", str(tmp), cand],
                              capture_output=True, text=True, timeout=3600)
        size = tmp.stat().st_size if tmp.exists() else 0
        if size == remote:
            tmp.rename(dst)
            tag = "downloaded" if not attempt else f"downloaded via {origin} after {attempt}"
            return f"ok ({tag} {remote/1e6:.0f}MB) {rel}"
        attempt += f"{origin}({size/1e6:.0f}MB) "
    return (f"WRONG SIZE: {rel} ({size} != {remote}) after {attempt}"
            f"{(' curl: ' + proc.stderr.strip()[:200]) if proc.returncode else ''}")

errors = []
with cf.ThreadPoolExecutor(max_workers=4) as ex:
    for res in ex.map(fetch, WANTED.items()):
        print("  ", res, flush=True)
        if not res.startswith("ok"):
            errors.append(res)

if errors:
    print("FAILURES:", errors, file=sys.stderr)
    sys.exit(1)
print("MODELS_OK")
PY
}

(
    fetch_models "$COMFY_MODEL_LIST" || { log "[ERROR] model download failed"; exit 1; }
    : > "$MODELS_CRITICAL_MARK"
    if [ -n "$COMFY_MODEL_LIST_DEFERRED" ]; then
        log "[4/5] deferred models: downloading behind the ready signal"
        if fetch_models "$COMFY_MODEL_LIST_DEFERRED"; then
            log "[4/5] deferred models ready"
        else
            # Not fatal: the endpoint is already serving, and a request for the
            # feature these belong to is what would notice. Left in the log so
            # a missing controlnet is read as this and not as a broken node.
            log "[WARN] deferred model download failed; feature requests may hit missing files"
        fi
    fi
) &
R2_PID=$!

# --- [3/4] custom nodes: wait for clones, then ONE combined pip ----------------
# The clones were forked before the model download; wait for them all. A failed
# clone is fatal exactly as before (set -e propagated it when it was serial).
CLONE_FAIL=0
for i in "${!CLONE_PIDS[@]}"; do
    if ! wait "${CLONE_PIDS[$i]}"; then
        log "[ERROR] git clone failed: ${CLONE_NAMES[$i]}"
        CLONE_FAIL=1
    fi
done
[ "$CLONE_FAIL" = 0 ] || exit 1

# One pip resolution for every requirements.txt plus the extras. Installing a
# single combined requirement set lets pip solve them together and downloads
# overlapping wheels once; before, each node's pip run resolved against the
# same cache separately. Per-node failures stay non-fatal (they were before).
REQS=()
for entry in "${NODES[@]:-}"; do
    IFS='|' read -r _repo name _rec norequirements <<< "$entry"
    if [ -z "$name" ] || [ -n "$norequirements" ]; then continue; fi
    if [ -f "$NODES_DIR/$name/requirements.txt" ]; then
        REQS+=(-r "$NODES_DIR/$name/requirements.txt")
    fi
done
if [ "${#REQS[@]}" -gt 0 ]; then
    # shellcheck disable=SC2086
    pip install $PIP_ARGS --no-build-isolation "${REQS[@]}" \
        || log "[WARN] some node requirements failed (continuing)"
fi
# shellcheck disable=SC2086
pip install $PIP_ARGS --no-build-isolation "${PIP_EXTRA[@]}" \
    || { log "[ERROR] failed installing node dependencies"; exit 1; }
hito "[3/5] custom nodes ready (${#NODES[@]})"

# Downloads by direct URL (public HuggingFace), in PARALLEL. They overlap with
# the pip pass above and the R2 download still running. A failure in any of them
# is fatal, exactly as it was when this loop ran serially.
url_fetch() {
    local url="$1" rel="$2" dst="$COMFY_DIR/$2"
    if [ -s "$dst" ]; then
        log "  ok (cache) $rel"
        return 0
    fi
    mkdir -p "$(dirname "$dst")"
    # same stall guard as the R2 fetcher: die after 30s under 10 KB/s and resume
    if curl -4 -fsSL --retry 3 --retry-all-errors \
            --speed-limit 10240 --speed-time 30 -C - -o "$dst.part" "$url"; then
        mv "$dst.part" "$dst"
        log "  ok (downloaded $(du -h "$dst" | cut -f1)) $rel"
        return 0
    fi
    rm -f "$dst.part"
    log "[ERROR] could not download $url"
    return 1
}
URL_PIDS=()
URL_RELS=()
for e in "${URL_FILES[@]:-}"; do
    IFS='|' read -r url rel <<< "$e"
    [ -n "$url" ] || continue
    url_fetch "$url" "$rel" &
    URL_PIDS+=($!)
    URL_RELS+=("$rel")
done

# Wait for the BLOCKING half of the R2 downloader (forked way above). Waiting
# on the pid would wait for the deferred half too, which is the whole thing
# this is avoiding; the marker is the boundary. If the job dies before writing
# it, the exit status is the real error and provisioning fails as it always
# did.
while [ ! -f "$MODELS_CRITICAL_MARK" ]; do
    if ! kill -0 "$R2_PID" 2>/dev/null; then
        wait "$R2_PID" || { log "[ERROR] model download failed"; exit 1; }
        break
    fi
    sleep 3
done

# The URL files are all behind --pose/--hands, so they are deferred on the same
# reasoning as the R2 feature models: nothing that answers a plain request is
# in here. They are not waited on, only reported, because a failure here costs
# a slow first pose request (the node fetches from HuggingFace mid-request),
# not a broken endpoint.
# They are left running: url_fetch already logs its own success and failure per
# file, so nothing is lost by not collecting the exit codes here, and a `wait`
# is what would put them back on the critical path.
if [ "${#URL_PIDS[@]}" -gt 0 ]; then
    log "[4/5] ${#URL_PIDS[@]} url file(s) still downloading behind the ready signal"
fi

hito "[4/5] models ready"

# --- [5/5] benchmark workflow for the pyworker --------------------------------
# The pyworker (BACKEND=comfyui-json) uses this JSON to measure GPU
# performance at startup. It stays intentionally light: 512x512 / 8 steps,
# no FaceDetailer or upscale. It only serves the benchmark; real requests
# send their own workflow_json.
BENCH_DIR="$WORKSPACE_DIR/vast-pyworker/workers/comfyui-json/misc"
read -r -d '' BENCH_JSON <<BENCH || true
{
  "1": {"class_type": "CheckpointLoaderSimple",
        "inputs": {"ckpt_name": "$BENCHMARK_CKPT"}},
  "60": {"class_type": "CLIPSetLastLayer",
         "inputs": {"clip": ["1", 1], "stop_at_clip_layer": -2}},
  "3": {"class_type": "CLIPTextEncode",
        "inputs": {"clip": ["60", 0], "text": "masterpiece, best quality, 1girl"}},
  "5": {"class_type": "CLIPTextEncode",
        "inputs": {"clip": ["60", 0], "text": "worst quality, blurry"}},
  "8": {"class_type": "EmptyLatentImage",
        "inputs": {"width": 512, "height": 512, "batch_size": 1}},
  "16": {"class_type": "KSampler",
         "inputs": {"seed": 1, "steps": 8, "cfg": 5.5, "sampler_name": "dpmpp_2m",
                    "scheduler": "karras", "denoise": 1.0,
                    "model": ["1", 0], "positive": ["3", 0],
                    "negative": ["5", 0], "latent_image": ["8", 0]}},
  "6": {"class_type": "VAEDecode", "inputs": {"samples": ["16", 0], "vae": ["1", 2]}},
  "7": {"class_type": "SaveImage",
        "inputs": {"filename_prefix": "benchmark", "images": ["6", 0]}}
}
BENCH

# start_server.sh clones the pyworker in parallel; we wait for the dir to exist
for _ in $(seq 1 120); do
    [ -d "$BENCH_DIR" ] && break
    sleep 1
done
if [ -d "$BENCH_DIR" ]; then
    echo "$BENCH_JSON" > "$BENCH_DIR/benchmark.json"
    hito "[5/5] benchmark.json written to $BENCH_DIR"
else
    log "[WARN] $BENCH_DIR did not appear in 120s; the default benchmark will be used"
fi

# example payload for the api-wrapper (useful for testing over SSH)
if [ -d /opt/comfyui-api-wrapper/payloads ]; then
    printf '{"input": {"request_id": "", "workflow_json": %s}}\n' "$BENCH_JSON" \
        > /opt/comfyui-api-wrapper/payloads/mizuki_smoke.json
    log "smoke test payload at /opt/comfyui-api-wrapper/payloads/mizuki_smoke.json"
fi

hito "=== PROVISIONING_OK ==="

# --- lifecycle sentinel -------------------------------------------------------
# The provisioning ends here, but ComfyUI still takes a while to open 18188
# and the container may die later (the autoscaler does restart_instance at
# ~791s of load). This process survives the script to notify about startup,
# crash and shutdown. The webhook is written to worker disk, never in the
# public .sh on R2.
if [ -n "$DISCORD_WEBHOOK" ]; then
    cat > "$WORKSPACE_DIR/discord_sentinel.sh" <<'SENT'
#!/bin/bash
# arg1: webhook  arg2: worker label
W="$1"; WHO="$2"; T0=$(date +%s)
el() { printf '%dm%02ds' $(( ($(date +%s)-T0)/60 )) $(( ($(date +%s)-T0)%60 )); }
send() {
    python3 - "$W" "$1" <<'PY' >/dev/null 2>&1 || true
import json, sys, urllib.request
url, content = sys.argv[1], sys.argv[2][:1900]
body = json.dumps({"content": content, "username": "mizuki-worker",
                   "allowed_mentions": {"parse": []}}).encode()
try:
    urllib.request.urlopen(urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json",
                                 # without a proper User-Agent Discord returns 403
                                 "User-Agent": "mizuki-provision/1.0"}), timeout=10).read()
except Exception:
    pass
PY
}
alive() { [ "$(curl -s -m 8 -o /dev/null -w '%{http_code}' http://127.0.0.1:18188/object_info)" = "200" ]; }
bye() { send ":octagonal_sign: **worker SHUTTING DOWN** \`$WHO\` (signal received after $(el) of life)"; exit 0; }
trap bye TERM HUP INT

# 1) wait for ComfyUI to open. 40 probes x 15 s = 10 min, which is the whole
# cold-start budget; the previous 60 min was dead patience, because the
# autoscaler gives up on a worker and recycles it long before then, so the
# failure message arrived after the evidence had been destroyed. A heads-up
# with the log tail goes out at the halfway mark, while the boot can still be
# watched live.
up=0
for i in $(seq 1 40); do
    if alive; then up=1; break; fi
    if [ "$i" = 20 ]; then
        send ":hourglass: **ComfyUI still not up** \`$WHO\` after $(el) - last log lines:"
        tail -n 25 /var/log/portal/comfyui.log 2>/dev/null | { c=$(cat); send "\`\`\`${c: -1400}\`\`\`"; }
    fi
    sleep 15
done
if [ "$up" = 1 ]; then
    send ":white_check_mark: **ComfyUI UP** \`$WHO\` in $(el) since provisioning ended"
else
    send ":warning: **ComfyUI did not open 18188** \`$WHO\` after $(el) - giving up"
    tail -n 25 /var/log/portal/comfyui.log 2>/dev/null | { c=$(cat); send "\`\`\`${c: -1400}\`\`\`"; }
    exit 0
fi

# 2) watch for crashes (3 consecutive failures = down; notified once per state)
failures=0; state=ok
while sleep 60; do
    if alive; then
        failures=0
        [ "$state" = down ] && { send ":arrows_counterclockwise: **ComfyUI recovered** \`$WHO\`"; state=ok; }
    else
        failures=$((failures+1))
        if [ "$failures" -ge 3 ] && [ "$state" = ok ]; then
            state=down
            send ":x: **ComfyUI stopped responding** \`$WHO\` (3 consecutive probes)"
            tail -n 25 /var/log/portal/comfyui.log 2>/dev/null | { c=$(cat); send "\`\`\`${c: -1400}\`\`\`"; }
        fi
    fi
done
SENT
    chmod +x "$WORKSPACE_DIR/discord_sentinel.sh"
    setsid nohup "$WORKSPACE_DIR/discord_sentinel.sh" "$DISCORD_WEBHOOK" "$WHO" \
        >/dev/null 2>&1 < /dev/null &
    disown 2>/dev/null || true
    log "Discord sentinel launched"
fi