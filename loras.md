# LoRAs de personaje

Notas para probar LoRAs de personaje sobre `waiIllustriousSDXL_v170`.

---

## Antes de descargar nada: pruébalo sin LoRA

`waiIllustrious` está basado en **Illustrious**, que se entrena con etiquetas de
Danbooru y **ya conoce muchos personajes nativamente**. `souryuu asuka langley`
es un tag bien representado.

En las pruebas de esta sesión, los sprites salieron con una Asuka perfectamente
reconocible **sin ninguna LoRA**, usando solo:

```
(souryuu asuka langley:1.3), (asuka langley:1.2), neon genesis evangelion,
long red hair, ahoge, (blue eyes:1.3), two side up, hair tubes, hair ribbons
```

Lo que sí derivaba entre frames **no era la cara, era la ropa**: la camiseta se
convertía en vestido a denoise alto, y el calzado cambiaba de un frame a otro.

Antes de meter una LoRA, prueba a **reforzar los tags de atuendo con pesos**.
En las pruebas, pasar de `white sneakers` a `(white sneakers:1.2)` más
`barefoot, black shoes, boots` en negativo resolvió la inconsistencia de calzado
por completo, y cuesta cero.

Una LoRA de personaje te servirá sobre todo si necesitas:
- Un diseño concreto (p. ej. el de *Rebuild* frente al de la serie original).
- Fijar un atuendo específico que los tags no describen bien.
- Subir el denoise por encima de 0.85 sin que la identidad se vaya.

---

## Candidatas para Illustrious

Ninguna de las dos está probada en este proyecto todavía. Los datos son los que
declaran sus páginas.

### 1. Souryuu Asuka Langley — Rebuild of Evangelion

<https://civitai.com/models/1259885/souryuu-asuka-langley-rebuild-of-evangelion-sdxl-lora>

| | |
|---|---|
| Fichero | `asuka_rebuild_v2-illustri` |
| Tamaño | 54,81 MB |
| Base | Illustrious |
| Fuerza recomendada | 1.0 |
| Publicada | 2025-02-19 |

Tags de activación declarados:

```
1girl, souryuu asuka langley, rebuild of evangelion, blue eyes, brown hair,
interface headset
```

Más tags específicos de atuendo (uniforme escolar, variantes de plugsuit).

> Ojo al `brown hair` de los triggers: es el diseño de *Rebuild*, no el pelo
> naranja de la serie original. Si buscas el look clásico, tenlo en cuenta o
> sobreescríbelo con `long red hair` con peso.

### 2. Rebuild of Evangelion — Asuka Langley Sohryu

<https://civitai.com/models/2226332/rebuild-of-evangelion-asuka-langley-sohryu>

| | |
|---|---|
| Tamaño | 109,15 MB |
| Base | Illustrious |
| Trigger | `Asuka` |
| Fuerza recomendada | 0.8 |
| Clip skip | 2 |
| Publicada | 2025-12-15 |

> El `clip skip 2` que pide **coincide con el `-2` que ya usamos** en el nodo
> `CLIPSetLastLayer` (nodo `60`). No hay que cambiar nada.

---

## Cómo añadir una LoRA al pipeline

### 1. Espejarla en R2

Descárgala de Civitai y súbela al bucket:

```powershell
python -c "import boto3, config; e=config.credenciales_r2(); \
  boto3.client('s3', endpoint_url=e['S3_ENDPOINT_URL'], \
    aws_access_key_id=e['S3_ACCESS_KEY_ID'], \
    aws_secret_access_key=e['S3_SECRET_ACCESS_KEY'], region_name='auto') \
  .upload_file('asuka_rebuild_v2-illustri.safetensors', e['S3_BUCKET_NAME'], \
    'comfy-stack/models/loras/asuka_rebuild_v2-illustri.safetensors')"
```

### 2. Añadirla al provisioning

Una línea en el array `MODELS` de `serverless_provision.sh`:

```bash
MODELS=(
  ...
  "comfy-stack/models/loras/asuka_rebuild_v2-illustri.safetensors|loras/asuka_rebuild_v2-illustri.safetensors"
)
```

Y desplegar:

```powershell
python renew_provisioning.py --update-workers
```

> **Espacio**: con 16 GB de disco quedan ~3 GB libres tras ControlNet. Una LoRA
> de 55–110 MB entra de sobra, pero si vas a añadir varias sube
> `VAST_DISK_SPACE` a 20 en `.env`.

### 3. Encadenarla en el workflow

Los workflows ya tienen un `LoraLoader` de estilo en el nodo `2`. Una LoRA de
personaje se **encadena** después: su `model` y `clip` vienen del nodo `2`, y
todo lo que consumía el `2` pasa a consumir el nodo nuevo.

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
  "_meta": { "title": "LoRA de personaje" }
}
```

Después hay que repuntar los consumidores:

- `CLIPSetLastLayer` (nodo `60`): `clip` pasa de `["2", 1]` a `["2b", 1]`
- Todos los `KSampler`: `model` pasa de `["2", 0]` a `["2b", 0]`
- `FaceDetailer` (si lo usas): `model` igual

En `sprites.py` y compañía, el sampler es el nodo `23`; en los de escena son el
`13`, el `43` y el `88`.

---

## Protocolo de prueba

Para saber si la LoRA aporta algo, hay que aislar la variable:

1. Genera una tanda **sin** LoRA con `sprites.py`, seed fija.
2. Genera la misma tanda **con** LoRA, misma seed y mismos prompts.
3. Compara en las dos cosas que fallaban: **consistencia del atuendo entre
   frames** y **deriva a denoise alto**.

Barre la fuerza: `0.6 / 0.8 / 1.0`. Una LoRA de personaje demasiado fuerte
tiende a imponer también la pose y el encuadre, que es justo lo que no quieres
cuando la pose la controla ControlNet.

Y ojo a un conflicto real: si la LoRA de personaje pelea con la de estilo del
nodo `2`, baja la de estilo de `0.5` a `0.3` antes de tocar la de personaje.
