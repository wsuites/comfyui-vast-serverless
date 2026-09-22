# Reproducing the results from scratch

Recipe and findings from the optimization session on `waiIllustriousSDXL_v170`.

---

## 1. Base recipe

| Parameter | Value |
|---|---|
| Checkpoint | `waiIllustriousSDXL_v170.safetensors` (6.9 GB) |
| Style LoRA | `stuffy_ai_style_ilxl_v2_goofy.safetensors` (model 0.5, clip 1.0) |
| Sampler | `euler` |
| Scheduler | `normal` |
| Steps | `30` |
| CFG | `6.0` |
| Clip skip | `-2` (**critical**: with `-1` this checkpoint produces black images) |
| Resolution | `1024x1024` (native latent) |
| Upscale | optional, 2x by tiles |
| FaceDetailer | denoise 0.35, guide 768, max 1024 |

---

## 2. Stack

### Hardware (Vast.ai)

- RTX 5080 / 5070 Ti / 4090, 16 GB VRAM minimum.
- ~$0.11-0.20/h depending on the offer. See the **Costs** section of the README.

### Software

- `vastai/comfy` container, Python 3.12.
- **torch 2.7.0+cu128** on RTX 50xx (Blackwell, sm_120); on Ampere/Ada, 2.6.0+cu126.
- Custom nodes: `ComfyUI-Impact-Pack` (+Subpack), `ComfyUI_UltimateSDUpscale`,
  `ComfyUI-RMBG`, `comfyui_controlnet_aux`.
- In interactive mode, start ComfyUI inside `tmux` to survive the SSH session
  closing:

  ```bash
  tmux new-session -d -s comfy "cd /workspace/ComfyUI && python3 main.py \
    --listen 127.0.0.1 --port 8188 --disable-auto-launch > /root/comfy.log 2>&1"
  ```

---

## 3. Optimization findings

These are the adjustments that changed the result the most, with the why.

### 3.1 Eye color

The model tended to paint the eyes a different color than requested,
especially after the FaceDetailer, which repaints the face and drifts without
color guidance.

- Positive: the color with weight, e.g. `(blue eyes:1.3)`.
- Negative: the unwanted colors and `heterochromia, mismatched eyes`.
- **Repeat the color in the FaceDetailer prompt.** It is the step that helps
  the most: without it, the repainted face ignores the color of the main
  prompt.
- CFG >= 6.5 degrades the eye color and mixes them. Use **CFG 5-6**.

### 3.2 Hands and feet

- Positive: `(perfect hands:1.4), five fingers, detailed hands` and
  `(perfect feet:1.2), five toes, detailed toenails`.
- Negative: `extra fingers, missing fingers, fused fingers, webbed fingers,
  bad hands, deformed hands, extra toes, missing toes, six toes`.
- With extended arms and visible palms, add `open hands, palms visible,
  fingers spread` and block `arms at side, crossed arms, hands behind back`.

### 3.3 Duplicate character

- Cause: identity weights too high, or latents out of the range where the
  model is stable.
- Fix: `solo, 1girl` at the start of the prompt, and in the negative
  `duplicate, 2girls, clone, multi-view, mirrored, extra person`.

### 3.4 Elements that appear unrequested

The model adds footwear, accessories or background on its own. They are fixed
by naming them explicitly in the negative. With weights if it insists: in the
sprite tests, `(white sneakers:1.2)` in the positive plus
`barefoot, black shoes, boots` in the negative solved a footwear
inconsistency that the unweighted prompt could not.

### 3.5 Skin noise

`clean skin, smooth skin` in the positive and `color noise, skin artifacts`
in the negative.

### 3.6 Resolution

- The sweet spot is the native latent **1024-1536**. `1024x1024` is the most
  stable.
- Do not generate at native 4K: grain appears and characters get cloned. The
  2x tiled upscale is the right path and does not alter identity.
- Descriptive natural language works better than loose tags for describing
  the scene; tags are better reserved for the character identity.

### 3.7 Framing

If the character comes out cut off, the cause is usually in the
**generation**, not the later composition. The framing must be requested
explicitly:

- Positive: `(full body:1.3), full body shot, entire body visible, feet visible`
- Negative: `cropped, out of frame, cut off, close-up, portrait, upper body`

---

## 4. Operational tips

- **Parameters via ssh stdin, never heredoc**: ssh joins the arguments with
  spaces and the remote shell re-partitions the prompt. One file per job to
  be able to parallelize without stepping on each other.
- **Maximum 3 concurrent SSH connections** to an instance; with 10 it
  saturates and drops.
- ComfyUI **must** run in tmux in interactive mode: `nohup`/`disown` die when
  the session closes.
- If the interruptible instance dies by bid (`exited`), raise the bid and
  relaunch: the disk persists.
- In serverless, worker restarts are normal. Everything not in the
  provisioning script is lost on every replacement.
- **In serverless, SSH itself is not guaranteed.** The host may filter the
  direct port *and* the `sshN.vast.ai` proxy may reject the key; that is a
  property of the renting, not of your setup. The way in is the outbound
  Cloudflare tunnel the `onstart` starts. See the README sections
  *Worker tunnel* and *SSH to the worker*.
