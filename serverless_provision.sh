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

# --- FUNCIONALIDADES ---------------------------------------------------------
# true/false por caracteristica. Cada una arrastra sus modelos, sus custom nodes
# y sus paquetes pip; apagar la que no uses ahorra disco y arranque en frio.
# El flag del cliente que activa cada una va en el comentario.
#
#   IMPORTANTE: apagar una aqui NO cambia los workflows. Si mandas un workflow
#   que usa algo apagado, ComfyUI falla con el nombre del modelo o del nodo.
FEAT_UPSCALE=true          # UltimateSDUpscale de wf.json            67 MB
FEAT_RMBG=true             # BiRefNet, recorta al personaje         ~1 GB
FEAT_CONTROLNET=true       # ControlNet union, lo pide cualquier --pose  2,5 GB
FEAT_POSE_DWPOSE=true      # --pose dwpose                          351 MB
FEAT_POSE_OPENPOSE=false   # --pose openpose: FALLA con anime       ~430 MB
FEAT_CARA=true             # --cara (FaceDetailer)                   52 MB
FEAT_MANOS_YOLO=true       # --manos yolo                            22 MB
FEAT_MANOS_MESH=true       # --manos mesh (MeshGraphormer)         1,37 GB

on() { [ "${1:-false}" = "true" ]; }

# --- Modelos de R2.  "<clave en el bucket>|<ruta relativa dentro de models/>" --
MODELS=(
  "comfy-stack/models/checkpoints/waiIllustriousSDXL_v170.safetensors|checkpoints/waiIllustriousSDXL_v170.safetensors"
  "comfy-stack/models/loras/stuffy_ai_style_ilxl_v2_goofy.safetensors|loras/stuffy_ai_style_ilxl_v2_goofy.safetensors"
)
if on "$FEAT_CARA"; then
  MODELS+=("comfy-stack/models/ultralytics/bbox/face_yolov8m.pt|ultralytics/bbox/face_yolov8m.pt")
fi
if on "$FEAT_UPSCALE"; then
  MODELS+=("comfy-stack/models/upscale_models/4x_NMKD-Siax_200k.pth|upscale_models/4x_NMKD-Siax_200k.pth")
fi
if on "$FEAT_RMBG"; then
  # Espejados en R2 para no depender de HuggingFace, que si no se bajarian en el
  # primer request. Los .py y el config.json son obligatorios: sin ellos no carga.
  MODELS+=(
    "comfy-stack/models/RMBG/BiRefNet/BiRefNet-general.safetensors|RMBG/BiRefNet/BiRefNet-general.safetensors"
    "comfy-stack/models/RMBG/BiRefNet/BiRefNet_config.py|RMBG/BiRefNet/BiRefNet_config.py"
    "comfy-stack/models/RMBG/BiRefNet/birefnet.py|RMBG/BiRefNet/birefnet.py"
    "comfy-stack/models/RMBG/BiRefNet/birefnet_lite.py|RMBG/BiRefNet/birefnet_lite.py"
    "comfy-stack/models/RMBG/BiRefNet/config.json|RMBG/BiRefNet/config.json"
  )
fi
if on "$FEAT_CONTROLNET"; then
  # Union: openpose, depth, canny... todo en un modelo. La pasada de manos
  # 'mesh' lo usa en modo depth, asi que tambien lo necesita.
  MODELS+=("comfy-stack/models/controlnet/controlnet-union-sdxl-1.0.safetensors|controlnet/controlnet-union-sdxl-1.0.safetensors")
fi

# --- Ficheros de R2 que NO van bajo models/. Ruta relativa a COMFY_DIR. -------
EXTRA_FILES=()
if on "$FEAT_POSE_OPENPOSE"; then
  EXTRA_FILES+=(
    "comfy-stack/custom_nodes/comfyui_controlnet_aux/ckpts/lllyasviel/Annotators/body_pose_model.pth|custom_nodes/comfyui_controlnet_aux/ckpts/lllyasviel/Annotators/body_pose_model.pth"
    "comfy-stack/custom_nodes/comfyui_controlnet_aux/ckpts/lllyasviel/Annotators/hand_pose_model.pth|custom_nodes/comfyui_controlnet_aux/ckpts/lllyasviel/Annotators/hand_pose_model.pth"
    "comfy-stack/custom_nodes/comfyui_controlnet_aux/ckpts/lllyasviel/Annotators/facenet.pth|custom_nodes/comfyui_controlnet_aux/ckpts/lllyasviel/Annotators/facenet.pth"
  )
fi

# --- Descargas por URL directa.  "<url>|<ruta relativa a COMFY_DIR>" ----------
# Para pesos publicos de HuggingFace que no merece la pena espejar en R2. A
# diferencia de MODELS/EXTRA_FILES no se comprueba el tamano contra un origen,
# solo se salta si el fichero ya existe.
AUX_CKPTS="custom_nodes/comfyui_controlnet_aux/ckpts"
URL_FILES=()
if on "$FEAT_POSE_DWPOSE"; then
  # Sin esto el nodo se los baja de HuggingFace EN PLENO REQUEST (medido: el
  # worker los tenia sin estar en el provisioning).
  URL_FILES+=(
    "https://huggingface.co/yzd-v/DWPose/resolve/main/yolox_l.onnx|$AUX_CKPTS/yzd-v/DWPose/yolox_l.onnx"
    "https://huggingface.co/yzd-v/DWPose/resolve/main/dw-ll_ucoco_384.onnx|$AUX_CKPTS/yzd-v/DWPose/dw-ll_ucoco_384.onnx"
  )
fi
if on "$FEAT_MANOS_YOLO"; then
  URL_FILES+=("https://huggingface.co/Bingsu/adetailer/resolve/main/hand_yolov8s.pt|models/ultralytics/bbox/hand_yolov8s.pt")
fi
if on "$FEAT_MANOS_MESH"; then
  # El tercer fichero de ese repo (control_sd15_inpaint_depth_hand, 722MB) NO
  # hace falta: es el ControlNet de SD1.5 y aqui se usa el union SDXL en depth.
  URL_FILES+=(
    "https://huggingface.co/hr16/ControlNet-HandRefiner-pruned/resolve/main/graphormer_hand_state_dict.bin|$AUX_CKPTS/hr16/ControlNet-HandRefiner-pruned/graphormer_hand_state_dict.bin"
    "https://huggingface.co/hr16/ControlNet-HandRefiner-pruned/resolve/main/hrnetv2_w64_imagenet_pretrained.pth|$AUX_CKPTS/hr16/ControlNet-HandRefiner-pruned/hrnetv2_w64_imagenet_pretrained.pth"
  )
fi

# --- Custom nodes.  "<repo git>|<directorio>|<recursive>|<norequirements>" ----
#   recursive       -> clonar con submodulos
#   norequirements  -> NO instalar su requirements.txt; las deps van en PIP_EXTRA
NODES=()
if on "$FEAT_CARA" || on "$FEAT_MANOS_YOLO"; then
  # FaceDetailer y UltralyticsDetectorProvider. FaceDetailer NO es especifico de
  # caras: con un bbox_detector de manos hace la pasada de manos.
  NODES+=(
    "https://github.com/ltdrdata/ComfyUI-Impact-Pack.git|ComfyUI-Impact-Pack||"
    "https://github.com/ltdrdata/ComfyUI-Impact-Subpack.git|ComfyUI-Impact-Subpack||"
  )
fi
if on "$FEAT_UPSCALE"; then
  NODES+=("https://github.com/ssitu/ComfyUI_UltimateSDUpscale.git|ComfyUI_UltimateSDUpscale|recursive|")
fi
if on "$FEAT_RMBG"; then
  # El requirements.txt de RMBG arrastra onnxruntime-gpu, groundingdino-py,
  # decord y SAM2/SAM3: ~2GB que no usamos. Solo queremos BiRefNet.
  NODES+=("https://github.com/1038lab/ComfyUI-RMBG.git|ComfyUI-RMBG||norequirements")
fi
if on "$FEAT_POSE_DWPOSE" || on "$FEAT_POSE_OPENPOSE" || on "$FEAT_MANOS_MESH"; then
  # anotadores de pose/depth para ControlNet; MeshGraphormer vive aqui dentro
  NODES+=("https://github.com/Fannovel16/comfyui_controlnet_aux.git|comfyui_controlnet_aux||")
fi

# --- Paquetes pip extra que necesitan los nodos de arriba --------------------
PIP_EXTRA=(opencv-python-headless scipy scikit-image spandrel boto3 huggingface-hub
           transformers)
if on "$FEAT_CARA" || on "$FEAT_MANOS_YOLO"; then
  PIP_EXTRA+=(ultralytics)
fi
if on "$FEAT_RMBG"; then
  PIP_EXTRA+=(transparent-background)
fi
if on "$FEAT_MANOS_MESH"; then
  # Sin estos, el nodo los instala por pip EN PLENO REQUEST.
  PIP_EXTRA+=(mediapipe trimesh)
fi

# Checkpoint que usa el workflow de benchmark (tiene que estar en MODELS)
BENCHMARK_CKPT="waiIllustriousSDXL_v170.safetensors"

# =============================================================================
# A partir de aqui no suele hacer falta tocar nada
# =============================================================================

WORKSPACE_DIR="${WORKSPACE:-/workspace}"
MODEL_LOG="${MODEL_LOG:-/var/log/portal/comfyui.log}"
mkdir -p "$(dirname "$MODEL_LOG")"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] provision: $1" | tee -a "$MODEL_LOG"; }

# --- notificaciones a Discord ------------------------------------------------
# El webhook NO se escribe aqui: este fichero se publica en una URL R2 publica.
# Llega por el entorno del template (-e DISCORD_WEBHOOK=...). Si no esta, todo
# lo de abajo es un no-op y el provisioning sigue igual.
DISCORD_WEBHOOK="${DISCORD_WEBHOOK:-}"
T0=$(date +%s)
WHO="${CONTAINER_ID:-${VAST_CONTAINERLABEL:-$(hostname)}}"
GPU=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
IP=$(curl -s -m 5 https://api.ipify.org 2>/dev/null || echo "?")

elapsed() { printf '%dm%02ds' $(( ($(date +%s) - T0) / 60 )) $(( ($(date +%s) - T0) % 60 )); }

# manda un mensaje. Blindado: red mala, 429 o webhook borrado nunca pueden
# tumbar el provisioning (de ahi el || true y el timeout corto).
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
                                 # sin User-Agent propio Discord devuelve 403
                                 "User-Agent": "mizuki-provision/1.0"}), timeout=10).read()
except Exception:
    pass
PY
}

# hito = al log del worker Y al canal
hito() { log "$1"; dc "[$WHO $(elapsed)] $1"; }

# vuelca las ultimas lineas del log dentro de un bloque de codigo
dc_log() {
    [ -n "$DISCORD_WEBHOOK" ] || return 0
    local n="${1:-40}" cola
    cola=$(tail -n "$n" "$MODEL_LOG" 2>/dev/null | tail -c 1500)
    dc "\`\`\`$(printf '%s' "$cola")\`\`\`"
}

on_err() {
    log "[ERROR] fallo en la linea $1 (exit $2) - el worker NO se marcara ready"
    dc ":x: **provisioning FALLO** \`$WHO\` linea $1 (exit $2) tras $(elapsed) - el worker no se marca ready"
    dc_log 40
    ERR_YA_AVISADO=1
}
trap 'on_err $LINENO $?' ERR

# --- poll de progreso ---------------------------------------------------------
# pip y las descargas de R2 pasan minutos sin escribir nada; sin esto el canal
# (y el log) parecen colgados cuando en realidad hay trabajo. Reporta cada 2 min
# el tamano de la cache de pip, de models/ y la ultima linea viva del log.
watch_progress() {
    local prev="" ahora
    while sleep 120; do
        ahora="pip:$(du -sm "${PIP_CACHE_DIR:-/tmp}" 2>/dev/null | cut -f1)MB"
        ahora="$ahora models:$(du -sm "${MODELS_DIR:-/tmp}" 2>/dev/null | cut -f1)MB"
        ahora="$ahora libre:$(df -h "$WORKSPACE_DIR" 2>/dev/null | awk 'NR==2{print $4}')"
        if [ "$ahora" = "$prev" ]; then
            dc ":hourglass: [$WHO $(elapsed)] $ahora _(sin cambios)_ · $(tail -n 1 "$MODEL_LOG" 2>/dev/null | tail -c 200)"
        else
            dc ":arrow_forward: [$WHO $(elapsed)] $ahora · $(tail -n 1 "$MODEL_LOG" 2>/dev/null | tail -c 200)"
        fi
        prev="$ahora"
    done
}

dc ":rocket: **provisioning ARRANCA** \`$WHO\` · ${GPU:-GPU?} · IP $IP"
log "=== inicio ==="
watch_progress & WATCH_PID=$!

# Los 'exit 1' explicitos (credenciales, ComfyUI no encontrado, pip) NO disparan
# el trap ERR, solo el EXIT. Aqui se cierra ese hueco: cualquier salida != 0 que
# no venga ya de on_err se reporta con su cola de log.
ERR_YA_AVISADO=0
on_exit() {
    local st=$?
    kill "$WATCH_PID" 2>/dev/null || true
    if [ "$st" != 0 ] && [ "$ERR_YA_AVISADO" = 0 ]; then
        dc ":x: **provisioning ABORTADO** \`$WHO\` (exit $st) tras $(elapsed)"
        dc_log 40
    fi
}
trap on_exit EXIT

# --- venv de la imagen -------------------------------------------------------
if [ -f /venv/main/bin/activate ]; then
    # shellcheck source=/dev/null
    . /venv/main/bin/activate
    log "venv /venv/main activado"
fi
# Cache de pip en disco persistente. El autoscaler mata la carga a los ~791s y
# hace restart_instance; con --no-cache-dir cada reinicio volvia a bajar los
# mismos wheels desde cero (142 MB directos, ~15 min en un host a 350 KB/s) y
# nunca cabia en el timeout: bucle infinito. Con cache, el 2o intento reaprovecha
# lo ya bajado y converge aunque el host tenga la red mala.
export PIP_CACHE_DIR="$WORKSPACE_DIR/.cache/pip"
mkdir -p "$PIP_CACHE_DIR"
# sin -q: en modo silencioso pip no escribe nada durante minutos y el log parece
# colgado cuando en realidad esta progresando.
PIP_ARGS="--cache-dir $PIP_CACHE_DIR --progress-bar off"
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
hito "[1/5] COMFY_DIR=$COMFY_DIR"

# --- [2/5] validar credenciales S3 antes de nada ------------------------------
missing=""
for v in S3_ACCESS_KEY_ID S3_SECRET_ACCESS_KEY S3_BUCKET_NAME S3_ENDPOINT_URL; do
    [ -n "${!v:-}" ] || missing="$missing $v"
done
if [ -n "$missing" ]; then
    log "[ERROR] faltan variables de entorno S3:$missing"
    exit 1
fi
hito "[2/5] credenciales S3 presentes (bucket=$S3_BUCKET_NAME)"

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

for entry in "${NODES[@]:-}"; do
    [ -n "$entry" ] || continue          # el array puede quedar vacio por un toggle
    IFS='|' read -r repo name recursive norequirements <<< "$entry"
    install_node "$repo" "$name" "$recursive" "$norequirements"
done

# shellcheck disable=SC2086
pip install $PIP_ARGS --no-build-isolation "${PIP_EXTRA[@]}" \
    || { log "[ERROR] fallo instalando dependencias de nodos"; exit 1; }
hito "[3/5] custom nodes listos (${#NODES[@]})"

# --- [4/5] modelos desde R2 --------------------------------------------------
export COMFY_MODELS_DIR="$MODELS_DIR"
# Se pasa a python un "clave|destino ABSOLUTO" por linea. MODELS va relativo a
# models/ y EXTRA_FILES relativo a COMFY_DIR, pero aqui ya se resuelven los dos.
COMFY_MODEL_LIST=$(
    # Ojo con el 'if': un '[ -n "$k" ] && echo' que falle en la ULTIMA vuelta
    # hace que la sustitucion entera salga con codigo 1 y, con set -e, mata el
    # provisioning. Pasa en cuanto un array queda vacio por un toggle en false.
    for e in "${MODELS[@]:-}"; do
        IFS='|' read -r k r <<< "$e"
        if [ -n "$k" ]; then echo "$k|$MODELS_DIR/$r"; fi
    done
    for e in "${EXTRA_FILES[@]:-}"; do
        IFS='|' read -r k r <<< "$e"
        if [ -n "$k" ]; then echo "$k|$COMFY_DIR/$r"; fi
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

# Descargas por URL directa (HuggingFace publico). Se hacen despues de R2 para
# que un fallo aqui no invalide lo ya bajado, pero cuentan igual: si falla una,
# el worker no se marca listo.
for e in "${URL_FILES[@]:-}"; do
    IFS='|' read -r url rel <<< "$e"
    [ -n "$url" ] || continue
    dst="$COMFY_DIR/$rel"
    if [ -s "$dst" ]; then
        log "  ok (cache) $rel"
        continue
    fi
    mkdir -p "$(dirname "$dst")"
    if curl -fsSL --retry 3 -o "$dst.part" "$url"; then
        mv "$dst.part" "$dst"
        log "  ok (descargado $(du -h "$dst" | cut -f1)) $rel"
    else
        rm -f "$dst.part"
        log "[ERROR] no se pudo bajar $url"
        exit 1
    fi
done

hito "[4/5] modelos listos"

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
    hito "[5/5] benchmark.json escrito en $BENCH_DIR"
else
    log "[WARN] $BENCH_DIR no aparecio en 120s; se usara el benchmark por defecto"
fi

# payload de ejemplo para el api-wrapper (util para probar por SSH)
if [ -d /opt/comfyui-api-wrapper/payloads ]; then
    printf '{"input": {"request_id": "", "workflow_json": %s}}\n' "$BENCH_JSON" \
        > /opt/comfyui-api-wrapper/payloads/mizuki_smoke.json
    log "payload de smoke test en /opt/comfyui-api-wrapper/payloads/mizuki_smoke.json"
fi

hito "=== PROVISIONING_OK ==="

# --- centinela de ciclo de vida ----------------------------------------------
# El provisioning termina aqui, pero ComfyUI tarda todavia en abrir el 18188 y
# el contenedor puede morir despues (el autoscaler hace restart_instance a los
# ~791s de carga). Este proceso sobrevive al script para avisar de encendido,
# caida y apagado. El webhook se escribe en disco del worker, nunca en el .sh
# publico de R2.
if [ -n "$DISCORD_WEBHOOK" ]; then
    cat > "$WORKSPACE_DIR/discord_sentinel.sh" <<'SENT'
#!/bin/bash
# arg1: webhook  arg2: etiqueta del worker
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
                                 # sin User-Agent propio Discord devuelve 403
                                 "User-Agent": "mizuki-provision/1.0"}), timeout=10).read()
except Exception:
    pass
PY
}
vivo() { [ "$(curl -s -m 8 -o /dev/null -w '%{http_code}' http://127.0.0.1:18188/object_info)" = "200" ]; }
bye() { send ":octagonal_sign: **worker APAGANDOSE** \`$WHO\` (senal recibida tras $(el) de vida)"; exit 0; }
trap bye TERM HUP INT

# 1) esperar a que ComfyUI abra (hasta 60 min)
arriba=0
for _ in $(seq 1 240); do
    if vivo; then arriba=1; break; fi
    sleep 15
done
if [ "$arriba" = 1 ]; then
    send ":white_check_mark: **ComfyUI ARRIBA** \`$WHO\` en $(el) desde el fin del provisioning"
else
    send ":warning: **ComfyUI no abrio el 18188** \`$WHO\` tras $(el)"
    tail -n 25 /var/log/portal/comfyui.log 2>/dev/null | { c=$(cat); send "\`\`\`${c: -1400}\`\`\`"; }
    exit 0
fi

# 2) vigilar caidas (3 fallos seguidos = caido; se avisa una sola vez por estado)
fallos=0; estado=ok
while sleep 60; do
    if vivo; then
        fallos=0
        [ "$estado" = caido ] && { send ":arrows_counterclockwise: **ComfyUI recuperado** \`$WHO\`"; estado=ok; }
    else
        fallos=$((fallos+1))
        if [ "$fallos" -ge 3 ] && [ "$estado" = ok ]; then
            estado=caido
            send ":x: **ComfyUI dejo de responder** \`$WHO\` (3 sondeos seguidos)"
            tail -n 25 /var/log/portal/comfyui.log 2>/dev/null | { c=$(cat); send "\`\`\`${c: -1400}\`\`\`"; }
        fi
    fi
done
SENT
    chmod +x "$WORKSPACE_DIR/discord_sentinel.sh"
    setsid nohup "$WORKSPACE_DIR/discord_sentinel.sh" "$DISCORD_WEBHOOK" "$WHO" \
        >/dev/null 2>&1 < /dev/null &
    disown 2>/dev/null || true
    log "centinela de Discord lanzado"
fi
