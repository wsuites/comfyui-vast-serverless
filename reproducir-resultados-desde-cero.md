# Reproducir los resultados desde cero

Receta y hallazgos de la sesión de optimización sobre `waiIllustriousSDXL_v170`.

---

## 1. Receta base

| Parámetro | Valor |
|---|---|
| Checkpoint | `waiIllustriousSDXL_v170.safetensors` (6.9 GB) |
| LoRA de estilo | `stuffy_ai_style_ilxl_v2_goofy.safetensors` (model 0.5, clip 1.0) |
| Sampler | `euler` |
| Scheduler | `normal` |
| Steps | `30` |
| CFG | `6.0` |
| Clip skip | `-2` (**crítico**: con `-1` este checkpoint produce imágenes negras) |
| Resolución | `1024x1024` (latente nativo) |
| Upscale | opcional, 2x por tiles |
| FaceDetailer | denoise 0.35, guide 768, max 1024 |

---

## 2. Stack

### Hardware (Vast.ai)

- RTX 5080 / 5070 Ti / 4090, 16 GB VRAM mínimo.
- ~$0.11–0.20/h según oferta. Ver la sección **Costes** del README.

### Software

- Contenedor `vastai/comfy`, Python 3.12.
- **torch 2.7.0+cu128** en RTX 50xx (Blackwell, sm_120); en Ampere/Ada, 2.6.0+cu126.
- Custom nodes: `ComfyUI-Impact-Pack` (+Subpack), `ComfyUI_UltimateSDUpscale`,
  `ComfyUI-RMBG`, `comfyui_controlnet_aux`.
- En modo interactivo, arrancar ComfyUI dentro de `tmux` para sobrevivir al
  cierre de la sesión SSH:

  ```bash
  tmux new-session -d -s comfy "cd /workspace/ComfyUI && python3 main.py \
    --listen 127.0.0.1 --port 8188 --disable-auto-launch > /root/comfy.log 2>&1"
  ```

---

## 3. Hallazgos de la optimización

Estos son los ajustes que más cambiaron el resultado, con el porqué.

### 3.1 Color de ojos

El modelo tendía a pintar los ojos de un color distinto al pedido, sobre todo
tras el FaceDetailer, que repinta la cara y sin guía de color deriva.

- Positivo: el color con peso, p. ej. `(blue eyes:1.3)`.
- Negativo: los colores no deseados y `heterochromia, mismatched eyes`.
- **Repetir el color en el prompt del FaceDetailer.** Es el paso que más
  ayuda: sin él, la cara repintada ignora el color del prompt principal.
- CFG ≥ 6.5 degrada el color de los ojos y los mezcla. Usar **CFG 5–6**.

### 3.2 Manos y pies

- Positivo: `(perfect hands:1.4), five fingers, detailed hands` y
  `(perfect feet:1.2), five toes, detailed toenails`.
- Negativo: `extra fingers, missing fingers, fused fingers, webbed fingers,
  bad hands, deformed hands, extra toes, missing toes, six toes`.
- Con brazos extendidos y palmas visibles, añadir `open hands, palms visible,
  fingers spread` y bloquear `arms at side, crossed arms, hands behind back`.

### 3.3 Personaje duplicado

- Causa: pesos de identidad demasiado altos, o latentes fuera del rango en el
  que el modelo es estable.
- Solución: `solo, 1girl` al principio del prompt, y en negativo
  `duplicate, 2girls, clone, multi-view, mirrored, extra person`.

### 3.4 Elementos que aparecen sin pedirlos

El modelo añade por su cuenta calzado, accesorios o fondo. Se corrigen
nombrándolos explícitamente en el negativo. Con pesos si insiste: en las
pruebas de sprites, `(white sneakers:1.2)` en positivo más
`barefoot, black shoes, boots` en negativo resolvió una inconsistencia de
calzado que el prompt sin pesos no lograba.

### 3.5 Ruido de piel

`clean skin, smooth skin` en positivo y `color noise, skin artifacts` en
negativo.

### 3.6 Resolución

- El punto dulce es el latente nativo **1024–1536**. `1024x1024` es lo más
  estable.
- No generar a 4K nativo: aparece grano y personajes clonados. El upscale 2x
  por tiles es la vía correcta y no altera la identidad.
- Lenguaje natural descriptivo funciona mejor que tags sueltos para describir
  la escena; los tags conviene reservarlos para la identidad del personaje.

### 3.7 Encuadre

Si el personaje sale cortado, la causa suele estar en la **generación**, no en
la composición posterior. Hay que pedir el encuadre explícitamente:

- Positivo: `(full body:1.3), full body shot, entire body visible, feet visible`
- Negativo: `cropped, out of frame, cut off, close-up, portrait, upper body`

---

## 4. Trucos operativos

- **Parámetros por stdin de ssh, nunca heredoc**: ssh une los argumentos con
  espacios y el shell remoto re-particiona el prompt. Un archivo por trabajo
  para poder paralelizar sin pisarse.
- **Máximo 3 conexiones SSH concurrentes** a una instancia; con 10 se satura y
  se cae.
- ComfyUI **debe** correr en tmux en modo interactivo: `nohup`/`disown` mueren
  al cerrar la sesión.
- Si la instancia interrumpible cae por puja (`exited`), subir el bid y
  relanzar: el disco persiste.
- En serverless, los reinicios de worker son normales. Todo lo que no esté en
  el script de provisioning se pierde en cada reemplazo.
