#!/bin/bash
# =============================================================================
# serverless_provision.sh - provisioning para el endpoint serverless "mizuki"
#
# Se ejecuta via PROVISIONING_SCRIPT (el provisioner de la imagen vastai/comfy
# lo descarga y lo corre ANTES de marcar el worker como listo). Si este script
# sale con codigo != 0, el worker NO se marca ready. Eso es intencionado.
#
# Requiere en el entorno (vienen del template / account env vars de Vast):
#   S3_ACCESS_KEY_ID  S3_SECRET_ACCESS_KEY  S3_BUCKET_NAME  S3_ENDPOINT_URL
#   S3_REGION (opcional, default "auto")
#
# Log: $MODEL_LOG (default /var/log/portal/comfyui.log)
# =============================================================================
set -euo pipefail

# =============================================================================
# CONFIGURACION - esto es lo unico que normalmente hay que tocar
# =============================================================================

# Modelos a bajar de R2.   "<clave en el bucket>|<ruta relativa dentro de models/>"
MODELS=(
  "comfy-stack/models/checkpoints/waiIllustriousSDXL_v170.safetensors|checkpoints/waiIllustriousSDXL_v170.safetensors"
  "comfy-stack/models/loras/stuffy_ai_style_ilxl_v2_goofy.safetensors|loras/stuffy_ai_style_ilxl_v2_goofy.safetensors"
  "comfy-stack/models/ultralytics/bbox/face_yolov8m.pt|ultralytics/bbox/face_yolov8m.pt"
  "comfy-stack/models/upscale_models/4x_NMKD-Siax_200k.pth|upscale_models/4x_NMKD-Siax_200k.pth"
  # Quitado de fondo con BiRefNet (nodo BiRefNetRMBG). Espejados en R2 para no
  # depender de HuggingFace, que si no se bajarian en el primer request.
  # Los .py y el config.json son obligatorios: sin ellos el modelo no carga.
  "comfy-stack/models/RMBG/BiRefNet/BiRefNet-general.safetensors|RMBG/BiRefNet/BiRefNet-general.safetensors"
  "comfy-stack/models/RMBG/BiRefNet/BiRefNet_config.py|RMBG/BiRefNet/BiRefNet_config.py"
  "comfy-stack/models/RMBG/BiRefNet/birefnet.py|RMBG/BiRefNet/birefnet.py"
  "comfy-stack/models/RMBG/BiRefNet/birefnet_lite.py|RMBG/BiRefNet/birefnet_lite.py"
  "comfy-stack/models/RMBG/BiRefNet/config.json|RMBG/BiRefNet/config.json"
  # ControlNet Union: openpose, depth, canny... todo en un modelo
  "comfy-stack/models/controlnet/controlnet-union-sdxl-1.0.safetensors|controlnet/controlnet-union-sdxl-1.0.safetensors"
)

# Ficheros que NO van bajo models/. Ruta relativa a COMFY_DIR.
# Los anotadores de controlnet_aux viven dentro del propio custom node; si no
# se pre-bajan aqui, el nodo los descarga de HuggingFace en pleno request.
EXTRA_FILES=(
  "comfy-stack/custom_nodes/comfyui_controlnet_aux/ckpts/lllyasviel/Annotators/body_pose_model.pth|custom_nodes/comfyui_controlnet_aux/ckpts/lllyasviel/Annotators/body_pose_model.pth"
  "comfy-stack/custom_nodes/comfyui_controlnet_aux/ckpts/lllyasviel/Annotators/hand_pose_model.pth|custom_nodes/comfyui_controlnet_aux/ckpts/lllyasviel/Annotators/hand_pose_model.pth"
  "comfy-stack/custom_nodes/comfyui_controlnet_aux/ckpts/lllyasviel/Annotators/facenet.pth|custom_nodes/comfyui_controlnet_aux/ckpts/lllyasviel/Annotators/facenet.pth"
)

# Custom nodes.  "<repo git>|<directorio>|<recursive>|<norequirements>"
#   recursive       -> clonar con submodulos
#   norequirements  -> NO instalar su requirements.txt; las deps van en PIP_EXTRA
NODES=(
  "https://github.com/ltdrdata/ComfyUI-Impact-Pack.git|ComfyUI-Impact-Pack||"
  "https://github.com/ltdrdata/ComfyUI-Impact-Subpack.git|ComfyUI-Impact-Subpack||"
  "https://github.com/ssitu/ComfyUI_UltimateSDUpscale.git|ComfyUI_UltimateSDUpscale|recursive|"
  # El requirements.txt de RMBG arrastra onnxruntime-gpu, groundingdino-py,
  # decord y SAM2/SAM3: ~2GB que no usamos. Solo queremos BiRefNet.
  "https://github.com/1038lab/ComfyUI-RMBG.git|ComfyUI-RMBG||norequirements"
  # anotadores de pose/depth para ControlNet
  "https://github.com/Fannovel16/comfyui_controlnet_aux.git|comfyui_controlnet_aux||"
)

# Paquetes pip extra que necesitan los nodos de arriba
PIP_EXTRA=(ultralytics opencv-python-headless scipy scikit-image spandrel boto3
           huggingface-hub transparent-background transformers)

# Checkpoint que usa el workflow de benchmark (tiene que estar en MODELS)
BENCHMARK_CKPT="waiIllustriousSDXL_v170.safetensors"

# =============================================================================
# A partir de aqui no suele hacer falta tocar nada
# =============================================================================

WORKSPACE_DIR="${WORKSPACE:-/workspace}"
MODEL_LOG="${MODEL_LOG:-/var/log/portal/comfyui.log}"
mkdir -p "$(dirname "$MODEL_LOG")"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] provision: $1" | tee -a "$MODEL_LOG"; }
on_err() { log "[ERROR] fallo en la linea $1 (exit $2) - el worker NO se marcara ready"; }
trap 'on_err $LINENO $?' ERR

log "=== inicio ==="

# --- venv de la imagen -------------------------------------------------------
if [ -f /venv/main/bin/activate ]; then
    # shellcheck source=/dev/null
    . /venv/main/bin/activate
    log "venv /venv/main activado"
fi
PIP_ARGS="--no-cache-dir -q"
[ -n "${VIRTUAL_ENV:-}" ] || PIP_ARGS="$PIP_ARGS --break-system-packages"

# --- [1/5] localizar ComfyUI -------------------------------------------------
COMFY_DIR=""
for d in "$WORKSPACE_DIR/ComfyUI" /opt/workspace-internal/ComfyUI /opt/ComfyUI /root/ComfyUI; do
    if [ -d "$d" ]; then COMFY_DIR="$d"; break; fi
done
if [ -z "$COMFY_DIR" ]; then
    log "[ERROR] no encuentro el directorio de ComfyUI"
    exit 1
fi
MODELS_DIR="$COMFY_DIR/models"
NODES_DIR="$COMFY_DIR/custom_nodes"
mkdir -p "$MODELS_DIR"/{checkpoints,loras,upscale_models} "$MODELS_DIR/ultralytics/bbox" "$NODES_DIR"
log "[1/5] COMFY_DIR=$COMFY_DIR"

# --- [2/5] validar credenciales S3 antes de nada ------------------------------
missing=""
for v in S3_ACCESS_KEY_ID S3_SECRET_ACCESS_KEY S3_BUCKET_NAME S3_ENDPOINT_URL; do
    [ -n "${!v:-}" ] || missing="$missing $v"
done
if [ -n "$missing" ]; then
    log "[ERROR] faltan variables de entorno S3:$missing"
    exit 1
fi
log "[2/5] credenciales S3 presentes (bucket=$S3_BUCKET_NAME)"

# --- [3/5] custom nodes ------------------------------------------------------
install_node() {
    local repo="$1" name="$2" recursive="${3:-}" norequirements="${4:-}"
    if [ -d "$NODES_DIR/$name" ]; then
        log "node ya presente: $name"
    else
        log "clonando $name"
        if [ -n "$recursive" ]; then
            git clone --depth 1 --recursive "$repo" "$NODES_DIR/$name"
        else
            git clone --depth 1 "$repo" "$NODES_DIR/$name"
        fi
    fi
    if [ -n "$norequirements" ]; then
        log "$name: se salta su requirements.txt (deps declaradas en PIP_EXTRA)"
    elif [ -f "$NODES_DIR/$name/requirements.txt" ]; then
        # shellcheck disable=SC2086
        pip install $PIP_ARGS --no-build-isolation -r "$NODES_DIR/$name/requirements.txt" \
            || log "[WARN] requirements de $name fallaron (continuo)"
    fi
}

for entry in "${NODES[@]}"; do
    IFS='|' read -r repo name recursive norequirements <<< "$entry"
    install_node "$repo" "$name" "$recursive" "$norequirements"
done

# shellcheck disable=SC2086
pip install $PIP_ARGS --no-build-isolation "${PIP_EXTRA[@]}" \
    || { log "[ERROR] fallo instalando dependencias de nodos"; exit 1; }
log "[3/5] custom nodes listos (${#NODES[@]})"

# --- [4/5] modelos desde R2 --------------------------------------------------
export COMFY_MODELS_DIR="$MODELS_DIR"
# Se pasa a python un "clave|destino ABSOLUTO" por linea. MODELS va relativo a
# models/ y EXTRA_FILES relativo a COMFY_DIR, pero aqui ya se resuelven los dos.
COMFY_MODEL_LIST=$(
    for e in "${MODELS[@]}"; do
        IFS='|' read -r k r <<< "$e"
        [ -n "$k" ] && echo "$k|$MODELS_DIR/$r"
    done
    for e in "${EXTRA_FILES[@]:-}"; do
        IFS='|' read -r k r <<< "$e"
        [ -n "$k" ] && echo "$k|$COMFY_DIR/$r"
    done
)
export COMFY_MODEL_LIST
python3 - <<'PY' || { log "[ERROR] fallo la descarga de modelos"; exit 1; }
import os, sys, concurrent.futures as cf
from pathlib import Path
import boto3
from boto3.s3.transfer import TransferConfig

models_dir = Path(os.environ["COMFY_MODELS_DIR"])
bucket = os.environ["S3_BUCKET_NAME"]

# clave en R2 -> ruta relativa dentro de models/ (viene del array MODELS del bash)
WANTED = {}
for line in os.environ["COMFY_MODEL_LIST"].splitlines():
    line = line.strip()
    if not line:
        continue
    key, _, rel = line.partition("|")
    WANTED[key.strip()] = rel.strip()
if not WANTED:
    print("no hay modelos declarados", file=sys.stderr)
    sys.exit(1)

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["S3_ENDPOINT_URL"],
    aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
    region_name=os.environ.get("S3_REGION", "auto"),
)
cfg = TransferConfig(multipart_threshold=64 * 1024**2, max_concurrency=8,
                     multipart_chunksize=64 * 1024**2)

def fetch(item):
    key, rel = item
    dst = Path(rel)          # el bash ya resuelve la ruta absoluta
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        remote = s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
    except Exception as e:
        return f"FALTA en R2: {key} ({e})"
    if dst.exists() and dst.stat().st_size == remote:
        return f"ok (cache) {rel}"
    tmp = dst.with_suffix(dst.suffix + ".part")
    s3.download_file(bucket, key, str(tmp), Config=cfg)
    if tmp.stat().st_size != remote:
        tmp.unlink(missing_ok=True)
        return f"TAMANO INCORRECTO: {rel}"
    tmp.rename(dst)
    return f"ok (descargado {remote/1e6:.0f}MB) {rel}"

errors = []
with cf.ThreadPoolExecutor(max_workers=4) as ex:
    for res in ex.map(fetch, WANTED.items()):
        print("  ", res, flush=True)
        if not res.startswith("ok"):
            errors.append(res)

if errors:
    print("FALLOS:", errors, file=sys.stderr)
    sys.exit(1)
print("MODELOS_OK")
PY
log "[4/5] modelos listos"

# --- [5/5] workflow de benchmark para el pyworker -----------------------------
# El pyworker (BACKEND=comfyui-json) usa este JSON para medir el rendimiento de
# la GPU al arrancar. Se mantiene ligero a proposito: 512x512 / 8 pasos, sin
# FaceDetailer ni upscale. Solo sirve para el benchmark; los requests reales
# mandan su propio workflow_json.
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

# start_server.sh clona el pyworker en paralelo; esperamos a que exista el dir
for _ in $(seq 1 120); do
    [ -d "$BENCH_DIR" ] && break
    sleep 1
done
if [ -d "$BENCH_DIR" ]; then
    echo "$BENCH_JSON" > "$BENCH_DIR/benchmark.json"
    log "[5/5] benchmark.json escrito en $BENCH_DIR"
else
    log "[WARN] $BENCH_DIR no aparecio en 120s; se usara el benchmark por defecto"
fi

# payload de ejemplo para el api-wrapper (util para probar por SSH)
if [ -d /opt/comfyui-api-wrapper/payloads ]; then
    printf '{"input": {"request_id": "", "workflow_json": %s}}\n' "$BENCH_JSON" \
        > /opt/comfyui-api-wrapper/payloads/mizuki_smoke.json
    log "payload de smoke test en /opt/comfyui-api-wrapper/payloads/mizuki_smoke.json"
fi

log "=== PROVISIONING_OK ==="
