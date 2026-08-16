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
| `reproducir-resultados-desde-cero.md` | Receta de parámetros y prompts que dan buenos resultados. |
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
| `construir_wf.py` | Genera las variantes del workflow |
| `wf_v_dwpose_hd.json` | **La configuración recomendada** |
| `escena.py` | Runner con `--sweep` de denoise |
| `frames.py` | Secuencia de poses en un set fijo |
| `correr.py` | Secuencia de movimiento (personaje cruzando la escena) |

### Lo que funciona y lo que no (todo medido)

| Técnica | Veredicto |
|---|---|
| Escenario fijo + collage | Base sólida |
| img2img **sin** máscara | ❌ **Deforma el escenario** |
| img2img **con** máscara | ✅ Escenario intacto a cualquier denoise |
| `OpenposePreprocessor` | ❌ Falla con anime, empeora la pose |
| `DWPreprocessor` | ✅ La mejor pose de las tres |
| Doble inpaint a 1024 | ✅ Mejora leve pero real |
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
tapa ese hueco.**

### Detalle: el doble inpaint

Si el personaje ocupa 640 px de un lienzo de 1024, en el latente son ~80×80
posiciones para toda la figura: cara, manos y pies se quedan sin píxeles. La
segunda pasada recorta la zona, la amplía a 1024, re-difunde a `denoise 0.35` y
la pega de vuelta. Mismo principio que el `FaceDetailer` pero para el cuerpo
entero.

La ganancia es visible al 100% (mechones de pelo definidos, pliegues de tela)
pero **modesta**. Cuanto menor sea el personaje dentro del plano, más compensa.

---

## Costes

Dos partidas independientes:

- **Disco**: se paga **24/7**, por GB **asignado** (no usado). Es lo que domina en
  un endpoint mayormente parado.
- **GPU**: solo mientras el worker corre.

### Disco

Medido en un worker real: la imagen + venv + 7 GB de modelos ocupan **22 GB**.
Por eso `DISK_SPACE=32` (10 GB de holgura para outputs y descargas). Pedir 60 GB
era pagar el triple de lo necesario.

`storage_cost` va en **$/GB/mes** y la mediana del mercado es **0.20**, o sea
$6.40/mes a 32 GB (y $12/mes a 60 GB). El filtro `storage_cost<=0.0625` lo topa
en $2/mes.

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
| Disco asignado | 60 GB | 32 GB |
| Coste de disco | $12.00/mes | ~$0.85/mes |
| GPU | $0.190/h | $0.136–0.201/h |
| Total a 60 h/mes | $23.40 | ~$12.90 |

> Las máquinas con disco barato tienden a cobrar **más** por hora. No se pueden
> optimizar las dos cosas a la vez: con disco ≤$2/mes, red <$1/TB y ≤$0.15/h
> simultáneamente hay **cero ofertas** en el mercado. Para un endpoint que está
> parado la mayor parte del tiempo, prioriza el disco. Si le vas a meter muchas
> horas, baja `dph_total` y acepta pagar más de disco.

### El `verified=true` fantasma

`--no-default` en el **template** no basta: al actualizar el workergroup, Vast
vuelve a inyectar `verified=true` en su `search_query`. Y ese filtro es
demoledor con presupuestos ajustados:

| | Ofertas ≤$0.15/h |
|---|---|
| con `verified=true` | **1** |
| sin él | **7** (mejor a $0.109/h) |

Con una sola oferta viable el autoscaler **relaja el tope de precio** y alquila
máquinas por encima de `dph_total`. Así entró un worker a $0.201/h teniendo el
tope en $0.15.

Por eso `renew_provisioning.py` pasa `--search_params ... -n` **también** al
workergroup, no solo al template. Para comprobar que la query quedó limpia:

```powershell
vastai show workergroups --raw    # 'verified' no debe aparecer en search_query
```

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
