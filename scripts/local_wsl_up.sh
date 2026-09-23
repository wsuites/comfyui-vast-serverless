#!/usr/bin/env bash
# =============================================================================
# local_wsl_up.sh - rebuild the local ComfyUI test rig on WSL2 + RTX 5070
# =============================================================================
# Reproduces, on this machine, the same image the pyworker-vast worker
# runs on Vast, so a workflow can be tested without renting a GPU.
#
# Run from WSL as root:
#     wsl.exe -d Debian -u root -- bash /mnt/c/path/to/comfy-vast/scripts/local_wsl_up.sh
#
# Or from inside WSL:
#     sudo bash scripts/local_wsl_up.sh
#
# Flags:
#     --no-models     skip the R2 checkpoint download (container only)
#     --teardown      remove everything this script creates, then exit
#
# Everything is idempotent: re-running it is safe and skips finished steps.
# =============================================================================
set -euo pipefail

# --- Configuration -----------------------------------------------------------
IMAGE="vastai/comfy:v0.30.0-cuda-13.2-py312"
CONTAINER="comfy-local"
PORT=18188
STATE_DIR="/opt/comfy-local"

# Repo root: the .env with the R2 credentials lives here. Resolved from this
# script's location so it works whether it is called from /mnt/c or from WSL.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$HERE")"
ENV_FILE="$REPO_ROOT/.env"

# Models to pull from R2: "<r2 key>|<path relative to models/>"
# Same bucket layout the provisioner uses, so keys stay in sync.
MODELS=(
  "comfy-stack/models/checkpoints/waiIllustriousSDXL_v170.safetensors|checkpoints/waiIllustriousSDXL_v170.safetensors"
)

WANT_MODELS=true
TEARDOWN=false
for arg in "$@"; do
  case "$arg" in
    --no-models) WANT_MODELS=false ;;
    --teardown)  TEARDOWN=true ;;
    *) echo "Unknown flag: $arg"; exit 2 ;;
  esac
done

log()  { echo -e "\n\033[1;36m==> $*\033[0m"; }
warn() { echo -e "\033[1;33m[WARN] $*\033[0m"; }
die()  { echo -e "\033[1;31m[ERROR] $*\033[0m" >&2; exit 1; }

# --- Teardown ----------------------------------------------------------------
if [ "$TEARDOWN" = true ]; then
  log "Removing container $CONTAINER"
  docker rm -f "$CONTAINER" 2>/dev/null || true
  log "Removing image $IMAGE"
  docker rmi "$IMAGE" 2>/dev/null || true
  log "Removing state dir $STATE_DIR (includes downloaded models)"
  rm -rf "$STATE_DIR"
  echo
  echo "Done. Other projects' containers were not touched."
  docker ps -a --format "table {{.Names}}\t{{.Status}}"
  exit 0
fi

[ "$(id -u)" -eq 0 ] || die "Run as root (sudo bash $0)"

# --- [1/6] GPU visible in WSL ------------------------------------------------
log "[1/6] Checking the GPU is visible in WSL"
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader \
  || die "nvidia-smi failed. Check the Windows NVIDIA driver / WSL GPU support."

# --- [2/6] nvidia-container-toolkit ------------------------------------------
log "[2/6] Ensuring nvidia-container-toolkit is installed"
if ! command -v nvidia-ctk >/dev/null 2>&1; then
  echo "Not found, installing..."
  apt-get install -y curl gnupg ca-certificates >/dev/null 2>&1 || true
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update -qq
  apt-get install -y nvidia-container-toolkit
else
  echo "Already installed: $(nvidia-ctk --version | head -1)"
fi

# GOTCHA: 'reload' instead of 'restart' on purpose. A restart would bounce every
# other container on this machine (xclusive_imgproxy, rella-postgres, ...).
if ! grep -q '"nvidia"' /etc/docker/daemon.json 2>/dev/null; then
  log "Registering the nvidia runtime with Docker (reload, not restart)"
  nvidia-ctk runtime configure --runtime=docker
  systemctl reload docker
  sleep 3
fi
docker info 2>/dev/null | grep -q 'nvidia' \
  || die "The nvidia runtime is not registered in Docker."

# --- [3/6] Image -------------------------------------------------------------
log "[3/6] Ensuring the image is present (14.2 GB, slow on a cold pull)"
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "Already present."
else
  docker pull "$IMAGE"
fi

# --- [4/6] Models from R2 ----------------------------------------------------
mkdir -p "$STATE_DIR"/{models/{checkpoints,loras,vae,upscale_models},output,input}

if [ "$WANT_MODELS" = true ]; then
  log "[4/6] Downloading models from R2"
  [ -f "$ENV_FILE" ] || die "No .env at $ENV_FILE (copy .env.example and fill it in)."

  # Written to disk instead of piped in so credentials never appear in `ps`.
  cat > "$STATE_DIR/fetch.py" <<'PYEOF'
import os, sys
from pathlib import Path
import boto3
from boto3.s3.transfer import TransferConfig

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["S3_ENDPOINT_URL"],
    aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
    region_name=os.environ.get("S3_REGION", "auto"),
)
bucket = os.environ["S3_BUCKET_NAME"]
cfg = TransferConfig(multipart_threshold=64 * 1024**2,
                     max_concurrency=8,
                     multipart_chunksize=64 * 1024**2)

for item in os.environ["COMFY_MODEL_LIST"].splitlines():
    item = item.strip()
    if not item:
        continue
    key, _, rel = item.partition("|")
    dst = Path("/models") / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    remote = s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
    if dst.exists() and dst.stat().st_size == remote:
        print(f"  cached  {rel} ({remote/1e9:.2f} GB)")
        continue
    print(f"  getting {rel} ({remote/1e9:.2f} GB)...", flush=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    s3.download_file(bucket, key, str(tmp), Config=cfg)
    if tmp.stat().st_size != remote:
        tmp.unlink(missing_ok=True)
        sys.exit(f"WRONG SIZE: {rel}")
    tmp.rename(dst)
    print(f"  ok      {rel}")
print("MODELS_OK")
PYEOF

  # No python3 in the WSL host, so this runs in a throwaway container.
  # Credentials go in via --env-file, never on the command line.
  printf '%s\n' "${MODELS[@]}" > "$STATE_DIR/model_list.txt"
  docker run --rm \
    --env-file "$ENV_FILE" \
    -e COMFY_MODEL_LIST="$(cat "$STATE_DIR/model_list.txt")" \
    -v "$STATE_DIR/models:/models" \
    -v "$STATE_DIR/fetch.py:/fetch.py:ro" \
    python:3.12-slim bash -lc \
    "pip install -q boto3 2>&1 | tail -1; python /fetch.py"
else
  log "[4/6] Skipping model download (--no-models)"
fi

# --- [5/6] Container ---------------------------------------------------------
log "[5/6] Starting the persistent container"
docker rm -f "$CONTAINER" 2>/dev/null || true

# Three things that cost time to find out, do not "simplify" them away:
#
#  1. --runtime=nvidia, NOT --gpus all. The daemon was reloaded rather than
#     restarted, so it never registered the device driver plugin that
#     --gpus all needs. --runtime=nvidia works without a restart.
#
#  2. -e SERVERLESS=true. Without it comfyui.sh removes itself from
#     portal.yaml and the port never opens: the container looks healthy while
#     supervisor loops "exited: comfyui (exit status 0; expected)" forever.
#
#  3. ComfyUI lives in /workspace/ComfyUI, NOT /opt/workspace-internal/ComfyUI.
#     Mounting the latter silently does nothing and the output dir stays empty.
docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  --runtime=nvidia \
  -e NVIDIA_VISIBLE_DEVICES=all \
  -e NVIDIA_DRIVER_CAPABILITIES=all \
  -e SERVERLESS=true \
  --shm-size=8g \
  -p "${PORT}:${PORT}" \
  -e COMFYUI_ARGS="--disable-auto-launch --listen 0.0.0.0 --port ${PORT}" \
  -e BENCHMARK_TEST_WIDTH=512 \
  -e BENCHMARK_TEST_HEIGHT=512 \
  -e BENCHMARK_TEST_STEPS=20 \
  -v "$STATE_DIR/models/checkpoints:/workspace/ComfyUI/models/checkpoints" \
  -v "$STATE_DIR/models/loras:/workspace/ComfyUI/models/loras" \
  -v "$STATE_DIR/models/upscale_models:/workspace/ComfyUI/models/upscale_models" \
  -v "$STATE_DIR/output:/workspace/ComfyUI/output" \
  -v "$STATE_DIR/input:/workspace/ComfyUI/input" \
  "$IMAGE" >/dev/null

# --- [6/6] Wait for the port -------------------------------------------------
log "[6/6] Waiting for ComfyUI on port $PORT (first boot takes ~2-4 min)"
for i in $(seq 1 60); do
  code=$(docker exec "$CONTAINER" curl -s -m 5 -o /dev/null \
           -w '%{http_code}' "http://127.0.0.1:${PORT}/object_info" 2>/dev/null || echo 000)
  if [ "$code" = "200" ]; then
    echo
    echo "ComfyUI is up: http://127.0.0.1:${PORT}"
    echo "  outputs -> $STATE_DIR/output  (\\\\wsl\$\\Debian$STATE_DIR\\output from Windows)"
    echo "  models  -> $STATE_DIR/models"
    echo
    echo "Queue a workflow:  python scripts/call_endpoint.py --workflow workflows/wf.json"
    echo "Tear it all down:  bash scripts/local_wsl_up.sh --teardown"
    exit 0
  fi
  printf '.'
  sleep 10
done

echo
warn "The port did not open within 10 minutes. Logs:"
docker exec "$CONTAINER" tail -30 /var/log/portal/comfyui.log 2>/dev/null \
  || docker logs --tail 30 "$CONTAINER"
exit 1
