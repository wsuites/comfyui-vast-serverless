# ComfyUI serverless en Vast.ai

Genera imágenes con `wf.json` (waiIllustrious SDXL v170 + LoRA stuffy + FaceDetailer
+ UltimateSDUpscale) en un endpoint serverless de Vast.ai. Los modelos y nodos se
provisionan desde Cloudflare R2 y las imágenes se suben ahí mismo.

> **Configuración**: todo lo específico de un despliegue (identificadores de
> Vast, bucket, URL de R2, credenciales) vive en `.env`, que **no se versiona**.
> Copia `.env.example` a `.env` y rellénalo. En la documentación los valores
> aparecen como `$VAST_ENDPOINT_ID`, `$VAST_TEMPLATE_ID`, etc.

## Ficheros

| Fichero | Qué es |
|---|---|
| `call_endpoint.py` | Cliente. Manda `wf.json` al endpoint y devuelve la URL de la imagen. |
| `wf.json` | Workflow en formato **API** de ComfyUI. Es lo que se envía en cada request. |
| `serverless_provision.sh` | Provisioning del worker. Copia de la que corre en R2 (`comfy-stack/scripts/serverless_provision.sh`). |
| `renew_provisioning.py` | Sube el provisioning a R2, regenera la URL presignada y actualiza template + workergroup. |
| `reproducir-resultados-desde-cero.md` | Receta de parámetros y hallazgos de prompt. |
| `loras.md` | LoRAs de personaje: candidatas, cómo añadirlas y cómo probarlas. |
| `.env` | Configuración y credenciales. **No se versiona, no compartir.** |
| `last_response.json` | Respuesta completa del último request (lo escribe el cliente). |

---

## Uso rápido

```powershell
cd <tu-repo>

# rápido, sin upscale
python call_endpoint.py --no-upscale --steps 20 `
  --prompt "aetherion, solo, 1girl, long red hair, (blue eyes:1.3), looking at viewer, standing"

# workflow completo: FaceDetailer + UltimateSDUpscale 2x -> 2048x2048
python call_endpoint.py --steps 30 --cfg 6 --seed 552827645330068 `
  --prompt "..." --negative "clothes, red eyes, bad hands, ..."
```

La URL de la imagen se imprime por stdout y la respuesta entera queda en
`last_response.json`.

### Flags

| Flag | Default | Notas |
|---|---|---|
| `--prompt` | el de `wf.json` | Prompt positivo (nodo `3`). |
| `--negative` | el de `wf.json` | Prompt negativo (nodo `5`). |
| `--seed` | aleatoria | Se imprime siempre, para poder reproducir. |
| `--width` / `--height` | 1024 / 1024 | Latente nativo. 1024x1024 es lo más estable. |
| `--batch` | 1 | Imágenes por request. |
| `--steps` | el de `wf.json` (35) | Ajusta también `end_at_step`. |
| `--cfg` | el de `wf.json` (5.5) | Por encima de 6.5 se degradan los ojos. |
| `--no-upscale` | off | Ver nota abajo. |
| `--workflow` | `wf.json` | Para usar otro workflow. |
| `--cost` | 100 | Unidades que descuenta el autoscaler por request. |
| `--timeout` | 900 | Segundos. Incluye el arranque en frío. |
| `--out` | `last_response.json` | Dónde volcar la respuesta. |

**Sobre `--no-upscale`:** no basta con ignorar los nodos del upscaler. Hay que
colgar el `SaveImage` (nodo `7`) directamente del FaceDetailer y **borrar** los
nodos `46` y `47`, o ComfyUI los ejecuta igual porque siguen en el grafo. El
script ya lo hace.

---

## Desde tu propio código

```python
import asyncio, json, uuid
from pathlib import Path
from vastai import Serverless

async def generar(workflow: dict) -> str:
    key = (Path.home() / ".config/vastai/vast_api_key").read_text().strip()
    client = Serverless(api_key=key)          # explícito, ver trampa abajo
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

wf = json.loads(Path("wf.json").read_text(encoding="utf-8"))
wf["3"]["inputs"]["text"] = "tu prompt"
wf["27"]["inputs"]["value"] = 12345           # seed
print(asyncio.run(generar(wf)))
```

> **Trampa del SDK:** `Serverless.__init__` tiene
> `api_key = os.environ.get("VAST_API_KEY", None)` como *valor por defecto de
> parámetro*, así que se evalúa **en el import del módulo**. Si defines la
> variable de entorno desde Python antes de instanciar, la ignora. O la exportas
> en la shell antes de lanzar el proceso, o pasas `api_key=` a mano.

### Contrato del payload

```json
{"input": {"request_id": "<uuid>", "workflow_json": { ...ComfyUI formato API... }}}
```

Opcionales dentro de `input`: `s3` (sobrescribe el destino de subida) y `webhook`
(respuesta asíncrona en vez de bloquear).

### Nodos de `wf.json`

| Nodo | Qué es |
|---|---|
| `1` | CheckpointLoaderSimple — `waiIllustriousSDXL_v170.safetensors` |
| `2` | LoraLoader — `stuffy_ai_style_ilxl_v2_goofy.safetensors` |
| `3` / `5` | Prompt positivo / negativo |
| `60` | CLIPSetLastLayer — **clip skip `-2`**, crítico: con `-1` salen imágenes negras |
| `16` | KSamplerAdvanced — `steps`, `cfg`, `sampler_name`, `scheduler` |
| `17` | FaceDetailer (Impact-Pack) |
| `20` | UltralyticsDetectorProvider — `bbox/face_yolov8m.pt` |
| `27` | Seed |
| `39` / `40` / `41` | Ancho / alto / batch |
| `46` / `47` | UpscaleModelLoader + UltimateSDUpscale (`4x_NMKD-Siax_200k.pth`) |
| `7` | SaveImage — es de donde el api-wrapper saca la imagen |

---

## Rendimiento medido (RTX 5080)

| Configuración | Tiempo |
|---|---|
| 832×1216, 12 pasos, sin upscale | 8,3 s |
| 1024×1024, 20 pasos, sin upscale | 13,4 s |
| 1024×1024, 30 pasos, FaceDetailer + upscale 2x → 2048×2048 | 32,6 s |
| Arranque en frío (worker parado → primer request) | ~2 min |

---

## Cómo está montado

```
endpoint <nombre> ($VAST_ENDPOINT_ID)
  └── workergroup $VAST_WORKERGROUP_ID
        └── template "<nombre> ComfyUI Serverless" (id $VAST_TEMPLATE_ID)
              image: vastai/comfy:v0.30.0-cuda-13.2-py312
              runtype: ssh          <- NO jupyter (ver Troubleshooting)
              env: -p 3000:3000 (pyworker)
                   -e COMFYUI_ARGS="--disable-auto-launch --port 18188"
                   -e HF_TOKEN=...
                   -e PROVISIONING_SCRIPT=<url de R2>
              onstart:
                   export SERVERLESS=true BACKEND=comfyui-json
                   entrypoint.sh &                      <- arranca supervisord
                   start_server.sh | bash               <- arranca el pyworker
```

Las credenciales `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_BUCKET_NAME`,
`S3_ENDPOINT_URL` y `S3_REGION` están como **variables de entorno de cuenta** en
Vast (`vastai show env-vars`), no en el template. Vast las inyecta en cada worker.

`serverless_provision.sh` corre vía `PROVISIONING_SCRIPT` **antes** de que el
worker se marque como listo. Instala Impact-Pack, Impact-Subpack y
UltimateSDUpscale, y baja de R2 estos 4 modelos:

```
comfy-stack/models/checkpoints/waiIllustriousSDXL_v170.safetensors   6.9 GB
comfy-stack/models/loras/stuffy_ai_style_ilxl_v2_goofy.safetensors   114 MB
comfy-stack/models/ultralytics/bbox/face_yolov8m.pt                   52 MB
comfy-stack/models/upscale_models/4x_NMKD-Siax_200k.pth               67 MB
```

Si el script falla sale con código != 0 y el worker **no** se marca listo, así
que los errores de provisioning se ven como error explícito en los logs y no como
un timeout mudo.

### Si cambias el provisioning

```powershell
python renew_provisioning.py                    # sube a R2 + actualiza template
python renew_provisioning.py --update-workers   # además fuerza a los workers vivos
```

Hace todo el ciclo: normaliza saltos de línea a LF, sube `serverless_provision.sh`
a R2, comprueba que la URL pública sirve exactamente eso, actualiza el template y
re-apunta el workergroup al hash resultante (que cambia en cada update).

`PROVISIONING_SCRIPT` apunta al **Public Development URL** del bucket, que es
permanente:

```
https://pub-XXXXXXXXXXXX.r2.dev/comfy-stack/scripts/serverless_provision.sh
```

Con `--presigned` se genera en su lugar una URL firmada de 7 días, por si algún
día se desactiva el acceso público del bucket.

Lee las credenciales de R2 de `.env` o del entorno. Vast enmascara los valores en
`vastai show env-vars` incluso con `-s`, así que no se pueden recuperar de ahí.

> Cloudflare devuelve **403** al User-Agent por defecto de `urllib`. `curl` y
> `wget` pasan sin problema, que es lo que usa el provisioner del worker; el
> script de verificación se hace pasar por curl. Si escribes tooling propio
> contra esa URL, acuérdate de mandar un User-Agent.

> Cambiar el template dispara un reemplazo de workers: el autoscaler levanta uno
> nuevo y provisiona de cero (unos minutos y unos céntimos). Es normal; se
> estabiliza solo en un worker frío.

---

## Qué se puede cambiar y cómo

Hay tres capas, y el coste de tocarlas es muy distinto. Cuanto más arriba, más barato.

| Quiero cambiar | Dónde | Comando | Coste |
|---|---|---|---|
| Prompt, seed, tamaño, steps, cfg, batch | flags del cliente | `python call_endpoint.py --...` | nada |
| El grafo del workflow (nodos, conexiones, otro workflow entero) | `wf.json` o `--workflow otro.json` | `python call_endpoint.py --workflow otro.json` | nada |
| Modelos, LoRAs, custom nodes | bloque `CONFIGURACION` de `serverless_provision.sh` | `python renew_provisioning.py --update-workers` | ~5 min de reprovisión |
| Imagen docker, puertos, env, GPU, disco | constantes de `renew_provisioning.py` | `python renew_provisioning.py` | reemplazo de worker |
| Cuántos workers y cuándo | endpoint | `vastai update endpoint $VAST_ENDPOINT_ID ...` | inmediato |

### El workflow no requiere redespliegue

Esto es lo importante: **el workflow viaja en cada request**. El worker no tiene
ninguna copia de `wf.json`; lo que se envía en `input.workflow_json` es lo que se
ejecuta. Puedes cambiar el grafo entero, usar otro workflow o mandar workflows
distintos en requests consecutivos sin tocar nada del despliegue.

El único límite es que los modelos y nodos que use ese workflow tienen que estar
en el worker. Si no, ComfyUI falla con el nombre del que falta.

### Encender y apagar funcionalidades del worker

Arriba de `serverless_provision.sh` hay un toggle `true`/`false` por
característica. Cada uno arrastra **sus modelos, sus custom nodes y sus paquetes
pip**, así que apagar lo que no uses ahorra disco y arranque en frío:

| Toggle | Flag del cliente | Tamaño | Estado |
|---|---|---|---|
| `FEAT_UPSCALE` | `UltimateSDUpscale` de `wf.json` | 67 MB | `true` |
| `FEAT_RMBG` | BiRefNet, recorta al personaje | ~1 GB | `true` |
| `FEAT_CONTROLNET` | lo pide cualquier `--pose` y `--manos mesh` | 2,5 GB | `true` |
| `FEAT_POSE_DWPOSE` | `--pose dwpose` | 351 MB | `true` |
| `FEAT_POSE_OPENPOSE` | `--pose openpose` | ~430 MB | **`false`** |
| `FEAT_CARA` | `--cara` | 52 MB | `true` |
| `FEAT_MANOS_YOLO` | `--manos yolo` | 22 MB | `true` |
| `FEAT_MANOS_MESH` | `--manos mesh` | 1,37 GB | `true` |

`FEAT_POSE_OPENPOSE` está en `false` porque `OpenposePreprocessor` falla con
anime y empeora la pose — ver la tabla de [Lo que funciona y lo que
no](#lo-que-funciona-y-lo-que-no-todo-medido). Apagarlo ahorra los tres
anotadores de `lllyasviel/Annotators`.

> ⚠️ Apagar un toggle **no cambia los workflows**. Si mandas un workflow que usa
> algo apagado, ComfyUI falla con el nombre del modelo o del nodo que falta.

**Dos cosas que estaban colgando de una descarga en pleno request** y ahora se
pre-bajan en el provisioning:

- Los modelos de **DWPose** (`yolox_l.onnx` + `dw-ll_ucoco_384.onnx`, 351 MB) no
  estaban en ningún array; el nodo se los bajaba de HuggingFace en el primer
  request. Se descubrió al inventariar un worker vivo: estaban en disco sin
  estar en el provisioning.
- `mediapipe` y `trimesh`, que el nodo de MeshGraphormer instala **por pip** si
  no los encuentra, también en pleno request.

> 🐛 **Cuidado al añadir toggles**: un `[ -n "$k" ] && echo ...` como última
> sentencia de un `for` hace que la sustitución `$( ... )` entera salga con
> código 1 cuando el array queda vacío, y con `set -euo pipefail` eso **mata el
> provisioning** y el worker nunca se marca listo. Usa `if ... then ... fi`.
> Los bucles sobre arrays llevan además `"${ARR[@]:-}"` y un `continue` de
> guardia.

### Añadir un modelo o un LoRA

1. Súbelo a R2 bajo `comfy-stack/models/<tipo>/`.
2. Añade una línea al array `MODELS` de `serverless_provision.sh`:

   ```bash
   MODELS=(
     ...
     "comfy-stack/models/loras/mi_lora.safetensors|loras/mi_lora.safetensors"
   )
   ```

   El formato es `<clave en el bucket>|<ruta relativa dentro de models/>`.
3. `python renew_provisioning.py --update-workers`

Los modelos ya descargados se saltan comparando tamaño con el de R2, así que
añadir uno nuevo no vuelve a bajar los 7 GB.

### Probar un modelo sin reprovisionar

Para **pruebas**, no merece la pena tocar `serverless_provision.sh`: eso dispara
el reemplazo del worker, un arranque en frío y volver a bajar los ~7 GB. Se baja
el modelo directamente al worker vivo por SSH:

```bash
ssh -i ~/.ssh/xcl -o IdentitiesOnly=yes -p <puerto> root@<ip>
curl -sL -o /workspace/ComfyUI/models/ultralytics/bbox/hand_yolov8s.pt \
  https://huggingface.co/Bingsu/adetailer/resolve/main/hand_yolov8s.pt
```

**ComfyUI recoge el fichero nuevo sin reiniciar**; se comprueba con
`curl -s http://127.0.0.1:18188/object_info/UltralyticsDetectorProvider`.

El puerto y la IP salen de `vastai show instances --raw` (campo `ports`); ojo
que el `ssh_host`/`ssh_port` que muestra la CLI es el proxy, y el mapeo directo
del 22 suele ser otro. La clave que funciona es `~/.ssh/xcl`.

> Dos callejones sin salida ya comprobados: `vastai attach ssh` sobre una
> instancia **ya corriendo** devuelve `success` pero **no propaga la clave**, y
> `vastai execute` solo funciona con instancias **paradas**.

> ⚠️ Lo bajado así **no sobrevive al reemplazo del worker**. Cuando la prueba
> convenza, hazlo permanente y lanza `renew_provisioning.py --update-workers`.

Para hacerlo permanente hay tres sitios en `serverless_provision.sh`, todos
colgando del toggle de su característica:

- **`MODELS`** / **`EXTRA_FILES`** — ficheros espejados en R2. Verifican tamaño
  contra el origen y se saltan si ya están.
- **`URL_FILES`** — descarga directa por URL, para pesos públicos de HuggingFace
  que no merece la pena espejar. Formato `<url>|<ruta relativa a COMFY_DIR>`.
  Solo se salta si el fichero ya existe; no compara tamaños.

Lo que hace falta para las variantes de manos, si se quieren hacer permanentes:

| Ruta | Ficheros | Tamaño |
|---|---|---|
| `hd3y` (yolo) | `Bingsu/adetailer` → `hand_yolov8s.pt` | 22 MB |
| `hd3m` (MeshGraphormer) | `hr16/ControlNet-HandRefiner-pruned` → `graphormer_hand_state_dict.bin` + `hrnetv2_w64_imagenet_pretrained.pth` | 856 + 513 MB |

El tercer fichero de ese repo, `control_sd15_inpaint_depth_hand_fp16.safetensors`
(722 MB), **no hace falta**: es el ControlNet de SD1.5 y aquí se usa el union
SDXL en modo depth. `mediapipe` y `trimesh` ya están en la imagen; si no
estuvieran, el nodo los instala por pip **en pleno request**.

### Añadir un custom node

Una línea en el array `NODES`, formato `<repo>|<directorio>|<recursive>`:

```bash
NODES=(
  ...
  "https://github.com/no-tal-cual/ComfyUI-LoQueSea.git|ComfyUI-LoQueSea|"
)
```

El tercer campo solo se rellena (con `recursive`) si el repo tiene submódulos,
como `ComfyUI_UltimateSDUpscale`. Si el nodo trae `requirements.txt` se instala
solo; si necesita paquetes extra, añádelos a `PIP_EXTRA`.

### Cambiar la GPU, la imagen o los puertos

Están como constantes arriba de `renew_provisioning.py`: `IMAGE`, `IMAGE_TAG`,
`SEARCH_PARAMS`, `HF_TOKEN`, `ONSTART`. Editas y corres `renew_provisioning.py`.
Por ejemplo, para aceptar también 4090:

```python
SEARCH_PARAMS = "gpu_name in [RTX_5080,RTX_4090] disk_space>=60 dph_total<0.40 verified=true rentable=true"
```

Ojo con bajar de 16 GB de VRAM: el workflow con FaceDetailer y upscale a 2048
no cabe cómodo.

### Cambiar el workflow del benchmark

`BENCHMARK_CKPT` y el bloque `BENCH_JSON` al final de `serverless_provision.sh`.
Se mantiene ligero a propósito (512×512, 8 pasos) porque solo sirve para que el
autoscaler mida el rendimiento de la GPU. Si lo haces pesado, alargas el arranque
en frío de todos los workers.

---

## Operación

```powershell
vastai show endpoints                    # estado del endpoint
vastai get endpt-workers $VAST_ENDPOINT_ID           # workers y su status
vastai get endpt-logs $VAST_ENDPOINT_ID              # log del autoscaler (lo más útil)
vastai get wrkgrp-logs $VAST_WORKERGROUP_ID             # log del workergroup
vastai show workergroups                 # config del workergroup
vastai show instances                    # instancias vivas

# parar el gasto del todo
vastai update endpoint $VAST_ENDPOINT_ID --max_workers 0 --cold_workers 0 --min_load 0

# volver a levantarlo
vastai update endpoint $VAST_ENDPOINT_ID --max_workers 1 --cold_workers 1 --min_load 0 --target_util 0.9
```

Config actual: `max_workers=1`, `cold_workers=0`, `min_load=0`. Un solo worker,
que se para solo cuando no hay carga y lo despierta el siguiente request.

> ⚠️ **No pongas `cold_workers=1` con `max_workers=1`.** En cuanto hay un worker
> caliente, el autoscaler ve 0 workers fríos, intenta crear uno, no cabe en la
> única plaza y lo destruye — cada 5 segundos, indefinidamente. En los logs se ve
> como `Creating 1 workers` / `Destroying 1 workers` en bucle. No se nota al
> principio porque hace falta que haya un worker caliente ocupando la plaza.
>
> Si quieres una plaza fría reservada de verdad, necesitas `max_workers=2`
> (1 caliente + 1 frío), asumiendo que puede haber 2 GPUs alquiladas.

---

## Escenas coherentes: personaje sobre escenario fijo

Pipeline para meter un personaje en un escenario que se mantiene idéntico entre
frames. Todo ocurre **dentro de un solo grafo**: no hay que subir imágenes al
worker, que en serverless es un lío.

```
ESCENARIO ──► KSampler(seed fija) ─────────────► [a_escena]  ← el "set"
                                                     │
PERSONAJE ──► KSampler ──► BiRefNet ──► escalar ─────┤
                              │                      │
                              │              ImageCompositeMasked
                              │                      │
                        DWPreprocessor          [b_collage]
                              │                      │
                    ControlNet Union            VAEEncode
                       (openpose)                    │
                              └──────────► SetLatentNoiseMask ──► KSampler
                                                                      │
                                                                 [c_fusionado]
                                                                      │
                                            recortar → x1024 → re-difundir → pegar
                                                                      │
                                                                 [f_detalle]
```

### Ficheros

| | |
|---|---|
| `construir_wf.py` | **Arma el workflow** encendiendo/apagando cada parte |
| `escena.py` | Runner con `--sweep` de denoise |
| `ab_detalle.py` | A/B de la segunda pasada, con tiempos |
| `frames.py` | Secuencia de poses en un set fijo |
| `correr.py` | Secuencia de movimiento (personaje cruzando la escena) |
| `wf_v_<pose>_<variante>.json` | La matriz pregenerada, para el A/B y para inspeccionar a mano |

### Interruptores

Todo el pipeline se enciende y se apaga con los mismos flags, y significan lo
mismo en `construir_wf.py`, `escena.py` y `correr.py`:

| Flag | Valores | Default | Qué hace |
|---|---|---|---|
| `--pose` | `none` / `openpose` / `dwpose` | `dwpose` | Guía de pose por ControlNet |
| `--detalle` | `no` / `hd` / `hd2` | `hd2` | 2ª pasada sobre el recorte del personaje |
| `--cara` | flag | off | `FaceDetailer` **dentro** de la 2ª pasada |
| `--manos` | `yolo` / `mesh` / `ambas` | off | Pasada de manos **dentro** de la 2ª pasada |
| `--sin-mascara` | flag | off | Quita `SetLatentNoiseMask` (deforma el escenario) |
| `--variante` | ver abajo | — | Atajo con nombre |
| `--workflow` | ruta | — | Usa un `.json` ya hecho e ignora todo lo anterior |

El **escenario → personaje → collage** es la base del pipeline y no se apaga:
es lo que hace el propio grafo. Lo que sí se apaga es la máscara que protege el
escenario en la fusión.

```powershell
# escribir un workflow concreto
python construir_wf.py --pose dwpose --detalle hd2 --cara --manos yolo -o mi_wf.json

# lo mismo, con el atajo
python construir_wf.py --variante hd3y -o mi_wf.json

# sin escribir fichero: los runners lo arman en memoria
python escena.py --pose dwpose --detalle hd2 --cara --manos yolo --denoise 0.85
python correr.py --variante hd3y --frames 8

# regenerar la matriz entera (3 poses x 7 variantes)
python construir_wf.py --matriz
```

Los atajos de `--variante` son combinaciones con nombre de los flags de arriba:

| Variante | Equivale a |
|---|---|
| `sd` | `--detalle no` |
| `hd` | `--detalle hd` (la versión original, solo como baseline) |
| `hd2` | `--detalle hd2` |
| `hd3` | `--detalle hd2 --cara` |
| `hd3y` | `--detalle hd2 --cara --manos yolo` |
| `hd3m` | `--detalle hd2 --cara --manos mesh` |
| `hd3ym` | `--detalle hd2 --cara --manos ambas` |

El builder rechaza las combinaciones imposibles en vez de generar un grafo roto:
`--cara` y `--manos` viven **dentro** de la 2ª pasada, así que necesitan
`--detalle hd2`; y `--sin-mascara` no es compatible con la 2ª pasada, porque el
recorte reutiliza esa misma máscara (nodo `53`).

### Lo que funciona y lo que no (todo medido)

| Técnica | Veredicto |
|---|---|
| Escenario fijo + collage | Base sólida |
| img2img **sin** máscara | ❌ **Deforma el escenario** |
| img2img **con** máscara | ✅ Escenario intacto a cualquier denoise |
| `OpenposePreprocessor` | ❌ Falla con anime, empeora la pose |
| `DWPreprocessor` | ✅ La mejor pose de las tres |
| Doble inpaint a 1024 (`hd`) | ⚠️ Mejora leve: deformaba el aspecto y usaba el prompt de escena |
| Doble inpaint a 1536 con prompt propio (`hd2`) | ✅ La ganancia grande. Cara y pelo dibujados de verdad |
| `FaceDetailer` sobre el recorte (`hd3`) | ✅ Mejora fina de ojos y pestañas, +3,5 s |
| Identidad entre frames | ⚠️ Sin resolver sin LoRA |

### Por qué la máscara es obligatoria

`img2img` re-difunde **toda** la imagen. Sin máscara, subir el denoise para
integrar mejor al personaje también reescribe el escenario: a 0.65 aparecían
molduras en el techo y cambiaban la lámpara y el marco de la ventana. Con
`SetLatentNoiseMask` limitado a la zona del personaje, el resto queda intacto y
puedes subir a 0.85 sin riesgo.

Dos parámetros importantes de la máscara:

- **`GrowMask expand`**: ensancha más allá de la silueta. Ese anillo es donde el
  modelo pinta la **sombra de contacto**. Con 48 aparecían artefactos en la
  pared; **24** va bien.
- **`FeatherMask 32`**: difumina el borde entre lo repintado y lo intacto.

### El compromiso denoise / identidad

| denoise | Escenario | Pose | Identidad |
|---|---|---|---|
| 0.45 | intacto | respeta el collage | se conserva |
| 0.55 | intacto | no corrige un collage malo | se conserva |
| 0.85 | intacto | **recoloca según el prompt** | **deriva** |

A denoise alto el modelo reinterpreta la pose dentro de la máscara, así que el
collage funciona como boceto y no hace falta colocar el recorte al píxel. El
precio es que la ropa y la cara derivan. **La LoRA del personaje es lo único que
tapa ese hueco** — ver [`loras.md`](loras.md), aunque antes conviene probar a
reforzar los tags de atuendo con pesos, que es gratis.

### Detalle: el doble inpaint

Si el personaje ocupa 640 px de un lienzo de 1024, en el latente son ~80×80
posiciones para toda la figura: cara, manos y pies se quedan sin píxeles. La
segunda pasada recorta la zona, la amplía, re-difunde y la pega de vuelta.
Mismo principio que el `FaceDetailer` pero para el cuerpo entero.

Hay tres variantes, que genera `construir_wf.py`:

| | Recorte ampliado a | Prompt | Denoise | Extra |
|---|---|---|---|---|
| `hd` | 1024×1024 **fijo** | el de la escena (nodos `40`/`41`) | 0.35 | — |
| `hd2` | 1536 por el lado largo, **aspecto intacto** | propio del recorte (nodos `93`/`94`) | 0.45 | — |
| `hd3` | igual que `hd2` | igual que `hd2` | 0.45 | `FaceDetailer` sobre el recorte |

**`hd` daba poco por tres razones, y las tres eran arreglables:**

1. **Deformaba.** El recorte real es `704×676` y se escalaba a `1024×1024`
   fijo: se estiraba un 4% de ancho, se re-difundía deformado y se devolvía
   estirado. `escalado()` en `construir_wf.py` lo escala ahora proporcional,
   redondeando a múltiplo de 8.
2. **Ampliaba poco.** 704→1024 es 1.45×; la cara seguía midiendo ~130 px. A
   1536 son 2.2× y la cara pasa de 300 px, que ya es territorio donde el modelo
   dibuja iris, pestañas y mechones separados.
3. **Prompt equivocado.** Usaba el positivo de la escena
   (*"sitting on the edge of the bed, bedroom, soft window light"*). En un
   recorte ya cerrado sobre el personaje, eso gasta capacidad repintando fondo.
   `hd2` usa un prompt propio, solo de personaje y detalle.

`hd3` mete además un `FaceDetailer` **después** de ampliar el recorte, que es
donde sale rentable: en el lienzo de 1024 la cara mide ~90 px y el detector
tiene poco con lo que trabajar; sobre el recorte a 1536 mide ~300 px. Toca
17k píxeles, todos dentro del bbox de la cara.

### Manos: `hd3y`, `hd3m`, `hd3ym`

Tres variantes más, todas encima de `hd3` y actuando sobre el recorte ya ampliado:

| | Cómo encuentra la mano | Qué guía la re-difusión | Denoise |
|---|---|---|---|
| `hd3y` | detector `bbox/hand_yolov8s.pt` | solo el prompt | 0.30 |
| `hd3m` | malla 3D de MeshGraphormer | **depth de la malla** por ControlNet union | 0.65 |
| `hd3ym` | las dos, malla primero y detector después | — | — |

`FaceDetailer` **no es específico de caras**: recorta lo que le marque el
`bbox_detector`. Con el detector de manos hace exactamente lo mismo. Por eso
`hd3y` son solo dos nodos.

MeshGraphormer (el pipeline **HandRefiner**) ajusta una malla 3D a la mano y
devuelve **dos** salidas: el mapa de profundidad y la máscara de la zona. Es una
**guía de geometría, no un corrector**: la pasada de inpaint hace falta igual.
Lo que sustituye es el detector, no la pasada.

#### Los dos detectores fallan de forma distinta

Medido sobre dos escenas, una con un puño pequeño y medio ocluido y otra con la
mano abierta y grande:

| | puño pequeño ocluido | mano abierta y grande |
|---|---|---|
| `hand_yolov8s` | detecta | detecta 2 manos, conf 0.87 |
| MeshGraphormer | **no detecta a ningún umbral** (0.6 / 0.3 / 0.15) | detecta ya a 0.6 |

> ⚠️ **MeshGraphormer falla en silencio.** Si no detecta, `get_depth` devuelve
> `None` y el nodo rellena con `np.zeros_like`: **depth negro y máscara vacía**,
> sin error ni aviso. La pasada entera se convierte en un ida y vuelta por el
> VAE que solo cuesta tiempo. Si usas esta ruta, mira siempre la salida
> `g_depth`: negra = no ha hecho nada.

El umbral por defecto del nodo (`detect_thr=0.6`) es además demasiado estricto
para anime — sobre la imagen completa detectaba 0 manos a 0.6 y 1 a 0.3 — así
que `construir_wf.py` lo baja a `MESH_DETECT_THR = 0.3`. Aun así no salva el
caso del puño ocluido. No es un problema de montaje: sobre una foto real el
mismo detector encuentra 2 manos a 0.6.

#### Qué elegir

- La mano **borrosa por falta de píxeles** ya la arregla en gran parte `hd2`/`hd3`,
  porque la re-difusión del cuerpo entero a 1536 también le toca. `hd3y` añade
  contraste y separación entre dedos.
- La mano **rota de anatomía** (dedos de más, fusionados) es el caso de
  MeshGraphormer. Pero si la geometría ya era correcta, el depth no tiene nada
  que corregir y solo cambia ligeramente la forma.

**Recomendación: `hd3y`.** Detecta en los dos escenarios, cuesta 7 s y no tiene
el modo de fallo mudo. Reserva `hd3m`/`hd3ym` para cuando veas manos rotas *y*
bien visibles.

### Coste de cada variante

Medido con `ab_detalle.py`, worker caliente y una seed distinta por variante
(con la misma seed ComfyUI cachea el grafo y los tiempos salen sin sentido):

| Variante | Tiempo | Sobre `sd` |
|---|---|---|
| `sd` (sin segunda pasada) | 9,0 s | — |
| `hd` | 12,5 s | +3,5 s |
| `hd2` | 18,5 s | +9,5 s |
| `hd3` | 22,0 s | +13,0 s |
| `hd3y` | 29,0 s | +20,0 s |
| `hd3m` | 35,2 s | +26,2 s |
| `hd3ym` | 43,0 s | +34,0 s |

**`hd2` es el que compensa** y es el default de `correr.py`: se lleva casi toda
la ganancia por 9,5 s. `hd3` añade una mejora fina en ojos y pestañas por 3,5 s
más — vale la pena en una imagen suelta, no tanto en una secuencia de 8 frames.

Para reproducir la comparación:

```powershell
python ab_detalle.py                                     # hd, hd2, hd3
python ab_detalle.py --variantes hd3,hd3y,hd3m,hd3ym --out ab_manos
```

> ⚠️ **Al cronometrar, dale una seed distinta a cada variante.** Con la misma
> seed ComfyUI cachea los nodos cuyos inputs no cambian y los tiempos salen sin
> sentido: en una tanda llegó a salir `hd3` en 4,5 s porque reusaba el grafo
> entero de la anterior.

Deja `ab_detalle_full.png` (imagen entera) y `ab_detalle_zoom.png` (cara al
200%), que es donde de verdad se ve la diferencia.

---

## Costes

Dos partidas independientes:

- **Disco**: se paga **24/7**, por GB **asignado** (no usado). Es lo que domina en
  un endpoint mayormente parado.
- **GPU**: solo mientras el worker corre.

### Disco

Medido en el worker real (2026-08-16): con `VAST_DISK_SPACE=16` la imagen + venv
+ los modelos ocupaban **14 GB de 16**, o sea 2,9 GB libres, y tras bajar los
pesos de MeshGraphormer quedaban **1,6 GB (91% usado)**. Demasiado justo, así que
`VAST_DISK_SPACE` está ahora en **18**, donde el worker se queda sobre el 78%.

**Cuánto disco pedir no afecta a la disponibilidad.** Las máquinas del pool
ofrecen entre 288 y 1.352 GB, así que `disk_space>=16`, `>=24` o `>=60` devuelven
exactamente las mismas 7 ofertas a los mismos precios. Lo único que cambia es la
factura, que es `storage_cost × GB`:

| `VAST_DISK_SPACE` | Coste al precio actual ($0,0267/GB/mes) | Tope con `storage_cost<=0.11` |
|---|---|---|
| 16 GB | $0,43/mes | $1,76/mes |
| **18 GB** | **$0,48/mes** | **$1,98/mes** |
| 24 GB | $0,64/mes | $2,64/mes |
| 32 GB | $0,85/mes | $3,52/mes |

18 GB deja el tope de disco en **$1,98/mes** sin perder ninguna oferta, y con
los 14 GB que ocupa el stack completo (MeshGraphormer incluido) quedan ~4 GB
libres. Si se añaden más modelos, subir a 24 cuesta 16 céntimos más al mes.

> No hay escalón de **VRAM** entre 16 y 24 GB: el mercado salta de una a otra sin
> nada en medio, así que pedir `gpu_ram>=18` es pedir `>=24`. Y ahí sí duele:
> con `storage_cost<=0.125` la 24 GB más barata se va a **$0,288/h** frente a
> $0,107/h. Esto es sobre **disco**, no sobre VRAM.

> ⚠️ **Comprueba siempre el disco real con `df -h /` en el worker antes de
> decidir**, no te fíes de este número. El README ya se quedó obsoleto una vez
> (decía 32 GB cuando la instancia tenía 16).

#### El techo de `storage_cost`

Está en **`0.11`**, que a 18 GB topa la factura de disco en $1,98/mes. No se baja
más a propósito:

| `storage_cost<=` | Ofertas | Tope a 24 GB |
|---|---|---|
| 0.0625 | **1** ⚠️ | $1,13/mes |
| 0.0834 | 2 | $1,50/mes |
| **0.11** | **7** | **$1,98/mes** |
| 0.125 (el anterior) | 7 | $2,25/mes |

Con **una sola oferta viable el autoscaler relaja el tope de precio y alquila por
encima de `dph_total`** (ver [El `verified=true` fantasma](#el-verifiedtrue-fantasma)),
que es justo la sorpresa que se quiere evitar. `0.11` mantiene las mismas 7
ofertas que el `0.125` anterior y baja el techo 27 céntimos: no cuesta nada.

`storage_cost` va en **$/GB/mes** y la mediana del mercado es **0.20**, o sea
$3.20/mes a 16 GB (y $12/mes a los 60 GB de antes). El filtro
`storage_cost<=0.0625` lo topa en $1/mes.

### Red

`inet_down_cost` va en **$/GB**. Un arranque en frío descarga ~22 GB (imagen +
modelos), así que a $1.30/TB son **~3 céntimos por arranque**. Exigir <$1/TB
recorta el abanico de 123 a 22 ofertas para ahorrar céntimos al mes: no compensa.
Por eso el filtro está en `inet_down_cost<=0.005` ($5/TB).

### Precio por hora

`dph_total<=0.25` es **obligatorio**, no cosmético. Entre las máquinas con disco
barato hay H100 a $4.26/h, y sin techo el autoscaler puede cogerlas.

### Antes y después

| | Antes | Ahora |
|---|---|---|
| Disco asignado | 60 GB | 18 GB |
| Coste de disco | $12.00/mes | ~$0.48/mes |
| GPU | $0.190/h | $0.136–0.201/h |
| Total a 60 h/mes | $23.40 | ~$12.90 |

> Las máquinas con disco barato tienden a cobrar **más** por hora. No se pueden
> optimizar las dos cosas a la vez: con disco ≤$2/mes, red <$1/TB y ≤$0.15/h
> simultáneamente hay **cero ofertas** en el mercado. Para un endpoint que está
> parado la mayor parte del tiempo, prioriza el disco. Si le vas a meter muchas
> horas, baja `dph_total` y acepta pagar más de disco.

### `verified=true`: quitarlo salía carísimo

Durante un tiempo este repo **quitaba** `verified=true` del filtro, porque con el
presupuesto de entonces dejaba una sola oferta:

| | Ofertas ≤$0.15/h |
|---|---|
| con `verified=true` y `storage_cost<=0.11` | **1** ⚠️ |
| sin `verified=true` | 7 (mejor a $0.109/h) |

El razonamiento era correcto en su mecanismo — **con una sola oferta viable el
autoscaler relaja el tope de precio** y alquila por encima de `dph_total`; así
entró un worker a $0.201/h teniendo el tope en $0.15 — pero la conclusión
trataba el síntoma. El daño lo hacía **quedarse sin abanico de ofertas**, no la
verificación.

**Y quitar `verified` tenía un coste oculto mucho peor: workers inservibles.**
Medido el 2026-08-16 con dos máquinas no verificadas seguidas (139268 y 147722):
ambas anunciaban puertos directos (`direct_port_count` 100 y 200) que en
realidad estaban **filtrados**. El worker provisiona bien, el pyworker arranca,
el autoscaler lo marca `idle` y enruta a `https://<ip>:<puerto>`… pero ese
puerto da *timeout* desde cualquier red externa, así que **el pyworker recibe
cero requests** (`num_requests_recieved: 0`) y cada llamada muere por timeout.

La palanca buena es `storage_cost`, que cuesta céntimos:

| Configuración | Ofertas | GPU más barata | Tope disco a 18 GB |
|---|---|---|---|
| sin `verified`, `storage<=0.11` | 7 | $0.107/h | $1.98/mes |
| **`verified`, `storage<=0.20`** ← actual | **11** | **$0.108/h** | $3.60/mes |
| `verified`, `storage<=0.11` | **1** ⚠️ | $0.134/h | $1.98/mes |

Aflojando el disco se recupera el abanico **y** el precio de GPU queda igual
($0.108 vs $0.107). Se paga como mucho $1.62/mes más de disco a cambio de que
las máquinas funcionen.

#### Cómo se diagnostica esto rápido

El síntoma es un request que se cuelga y muere con
`TimeoutError: Timed out after 61.4s waiting for worker`, con el worker en
`idle`. Comprobaciones, en orden:

```powershell
# 1. que URL entrega el autoscaler, y esta abierto ese puerto?
python -c "import socket;socket.create_connection(('<ip>',<puerto>),10)"

# 2. desde fuera de tu red, por si el bloqueo es tuyo
curl -s "https://check-host.net/check-tcp?host=<ip>:<puerto>&max_nodes=3"
```

Dentro del worker, el pyworker habla **HTTPS**, no HTTP: `curl -k
https://127.0.0.1:3000/health` devuelve **404** cuando está sano (esa ruta no
existe, pero responder ya prueba que vive). Con `http://` da *Empty reply from
server* y parece caído sin estarlo. Y `/workspace/pyworker.log` trae un
`num_requests_recieved` que dice si le llega algo.

### Interruptible: no está soportado

Los workergroups serverless **solo usan on-demand**. Probado el 2026-08-15:
poniendo `--launch_args "--bid_price 0.15"`, la instancia creada sale con
`is_bid=False`. Las instancias interruptibles (que salen a la mitad de precio)
solo se pueden usar fuera del serverless, con `vastai create instance --bid_price`.

---

## Troubleshooting

### El worker cicla `model_loading -> error (timed out loading after 795s) -> reboot`

El síntoma clásico de que **el servicio nunca arranca**. Casi siempre es el modo
de lanzamiento: si el template está en **Jupyter** (o ssh sin llamar a
`entrypoint.sh`), Vast sustituye el ENTRYPOINT de la imagen por `/.launch`, que
solo levanta sshd y jupyter. El supervisord de `vastai/comfy` nunca corre, así que
no hay ComfyUI ni pyworker y el autoscaler no recibe señal de listo.

Cómo confirmarlo, entrando por SSH al worker:

```bash
ps -p 1 -o cmd=            # si dice "bash /.launch" -> es esto
supervisorctl status       # "no such file" -> supervisord no está corriendo
ss -lntp                   # solo 8080 y 22; falta 18188 (ComfyUI) y 3000 (pyworker)
ls /var/log/supervisor /var/log/portal   # vacíos
```

Y desde fuera: `image_runtype` en `vastai show instances --raw` dice
`jupyter_direc ...` en vez de `ssh`.

Arreglo: que el `onstart` del template llame a `entrypoint.sh &` y luego a
`start_server.sh`, como arriba.

### `HF_TOKEN must be set when BACKEND is set!`

Validación de `start_server.sh` del pyworker. Exige que exista `HF_TOKEN` si
`BACKEND` está definido, aunque no descargues nada de HuggingFace. Ponlo en el
env del template.

### El worker aparece como `stopped`

Normal, no es un fallo. Con `min_load=0` el autoscaler apaga los workers cuando
no hay carga y los deja en frío. El siguiente request lo despierta.

### `invalid template hash or id`

`vastai update template` **cambia el `hash_id`**. Después de tocar el template hay
que re-apuntar el workergroup al hash nuevo:

```powershell
vastai search templates "creator_id=$VAST_CREATOR_ID"    # sacar el hash actual
vastai update workergroup $VAST_WORKERGROUP_ID --endpoint_id $VAST_ENDPOINT_ID --template_hash <HASH> --template_id $VAST_TEMPLATE_ID
```

### Imágenes negras

Clip skip. El nodo `60` (`CLIPSetLastLayer`) tiene que estar en `-2`. Con `-1`,
`waiIllustriousSDXL_v170` produce imágenes negras.

### Falta un modelo o un nodo custom

El request falla en ComfyUI con el nombre del nodo o del fichero. Añádelo a
`serverless_provision.sh` (lista `WANTED` para modelos, `install_node` para nodos),
súbelo a R2 y lanza `vastai update workers $VAST_WORKERGROUP_ID`.

---

## Mantenimiento pendiente

- [ ] **`PROVISIONING_SCRIPT` es una presigned que caduca a los 7 días** (máximo
      que permite SigV4). Mitigado: `python renew_provisioning.py` renueva el
      ciclo entero en un comando, pero hay que acordarse cada semana.
      Para quitarse el problema del todo: dashboard de Cloudflare → R2 → bucket
      `mizuki` → Settings → *Public Development URL* → Enable, y cambiar
      `PROVISIONING_SCRIPT` por
      `https://pub-XXXX.r2.dev/comfy-stack/scripts/serverless_provision.sh`
      (permanente y sin query string).
- [ ] **`HF_TOKEN=hf_placeholder_not_used`** en el template. Sustituir por uno
      real si algún día se añaden descargas desde HuggingFace.
- [ ] Las URLs de las imágenes generadas caducan a los 7 días. Si hay que
      conservarlas, descargarlas o moverlas dentro de R2.
