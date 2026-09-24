import type { Command } from 'commander'

import { float, int } from '../lib/program.ts'

/**
 * The generation surface, mirrored from `scripts/call_endpoint.py` and the
 * `JobRequest` model in `webapp/server.py`.
 *
 * Defined once and attached to both `cv gen` and `cv job submit` so the two
 * paths into the same pipeline cannot drift: one goes straight to Python, the
 * other through the FastAPI the web UI uses, and an operator should not have
 * to remember which flags exist on which.
 */
export const RMBG_MODELS = ['birefnet', 'birefnet-hr', 'birefnet-lite', 'birefnet-portrait', 'inspyrenet'] as const

/** Arms from `scripts/ab_modelo.py:ARMS`. */
export const ARMS = ['wai', 'wai_nolora', 'wai_beta', 'wai_beta57', 'anima'] as const

export interface GenCliOptions {
  negative?: string
  seed?: number
  width?: number
  height?: number
  batch?: number
  steps?: number
  cfg?: number
  family?: string
  animaModel?: string
  lora?: number
  face?: boolean
  faceCap?: number
  detailPrompt?: string
  detailNegative?: string
  upscale?: boolean
  removeBg?: string
  bgRefine?: boolean
  bgSensitivity?: number
  bgBlur?: number
  bgOffset?: number
  cost?: number
  timeout?: number
  workflow?: string
}

export function genOptions(cmd: Command): Command {
  return cmd
    .option('-n, --negative <text>', 'negative prompt; blank keeps the workflow default')
    .option('-s, --seed <n>', 'seed; omit for a random one', (v) => int(v, '--seed'))
    .option('-W, --width <px>', 'width', (v) => int(v, '--width'))
    .option('-H, --height <px>', 'height', (v) => int(v, '--height'))
    .option('-b, --batch <n>', 'images per request', (v) => int(v, '--batch'))
    .option('--steps <n>', 'sampler steps', (v) => int(v, '--steps'))
    .option('--cfg <x>', 'classifier-free guidance scale', (v) => float(v, '--cfg'))
    .option('--family <name>', `model family: ${['wai', 'anima'].join(' | ')}`)
    .option('--anima-model <file>', 'UNET file, with --family anima')
    .option('--lora <strength>', 'style LoRA strength, wai only; 0 removes the node', (v) => float(v, '--lora'))
    .option('--face', 'run the FaceDetailer pass; off unless asked for')
    .option('--no-face', 'skip the face pass - the default, kept so the old spelling still parses')
    .option('--face-cap <n>', 'repaint at most n faces, largest first; 0 lifts the cap', (v) => int(v, '--face-cap'))
    .option('--detail-prompt <text>', 'prompt the face pass repaints with (wf.json ships "red eyes")')
    .option('--detail-negative <text>', 'negative for the face pass')
    .option('--upscale', 'run the upscale pass; off unless asked for')
    .option('--no-upscale', 'skip the upscale pass - the default, kept so the old spelling still parses')
    .option('--remove-bg <model>', `background removal: ${RMBG_MODELS.join(' | ')}`)
    .option('--bg-refine', 'refine the matte edge; better on hair, slower')
    .option('--bg-sensitivity <x>', 'background matte sensitivity', (v) => float(v, '--bg-sensitivity'))
    .option('--bg-blur <px>', 'mask blur', (v) => int(v, '--bg-blur'))
    .option('--bg-offset <px>', 'mask offset', (v) => int(v, '--bg-offset'))
}

/** Validate the enum-shaped flags before anything is submitted. */
export function validateGenOptions(o: GenCliOptions): string[] {
  const problems: string[] = []
  if (o.family !== undefined && o.family !== 'wai' && o.family !== 'anima') {
    problems.push(`--family must be wai or anima, not ${JSON.stringify(o.family)}`)
  }
  if (o.removeBg !== undefined && !(RMBG_MODELS as readonly string[]).includes(o.removeBg)) {
    problems.push(`--remove-bg must be one of ${RMBG_MODELS.join(', ')}`)
  }
  return problems
}

/**
 * Turn CLI options into the argv `scripts/call_endpoint.py` expects.
 *
 * The two expensive passes are opt-in: absent means off, so the negation is
 * emitted unless `--upscale`/`--face` asked for it. Sent on every call rather
 * than left out, because the wire format is still `no_upscale`/`no_face` and
 * the workflow's own default is the opposite of this one. Saying it every time
 * is what keeps the answer from depending on which end is asked.
 */
export function callEndpointArgs(o: GenCliOptions, prompt: string | undefined): string[] {
  const args: string[] = []
  const push = (flag: string, value: unknown) => {
    if (value === undefined || value === null) return
    args.push(flag, String(value))
  }

  if (prompt) push('--prompt', prompt)
  push('--negative', o.negative)
  push('--seed', o.seed)
  push('--width', o.width)
  push('--height', o.height)
  push('--batch', o.batch)
  push('--steps', o.steps)
  push('--cfg', o.cfg)
  push('--family', o.family)
  push('--anima-model', o.animaModel)
  push('--lora', o.lora)
  push('--detail-prompt', o.detailPrompt)
  push('--detail-negative', o.detailNegative)
  push('--remove-bg', o.removeBg)
  push('--bg-sensitivity', o.bgSensitivity)
  push('--bg-blur', o.bgBlur)
  push('--bg-offset', o.bgOffset)
  push('--face-cap', o.faceCap)
  push('--cost', o.cost)
  push('--timeout', o.timeout)
  push('--workflow', o.workflow)

  if (o.upscale !== true) args.push('--no-upscale')
  if (o.face !== true) args.push('--no-face')
  if (o.bgRefine) args.push('--bg-refine')

  return args
}

/** The same options as the `JobRequest` body `webapp/server.py` accepts. */
export function jobParamsFromOptions(o: GenCliOptions, prompt: string | undefined): Record<string, unknown> {
  const p: Record<string, unknown> = {}
  const set = (key: string, value: unknown) => {
    if (value !== undefined && value !== null) p[key] = value
  }

  set('prompt', prompt)
  set('negative', o.negative)
  set('seed', o.seed)
  set('width', o.width)
  set('height', o.height)
  set('batch', o.batch)
  set('steps', o.steps)
  set('cfg', o.cfg)
  set('family', o.family)
  set('anima_model', o.animaModel)
  set('lora', o.lora)
  set('detail_prompt', o.detailPrompt)
  set('detail_negative', o.detailNegative)
  set('remove_bg', o.removeBg)
  set('bg_sensitivity', o.bgSensitivity)
  set('bg_blur', o.bgBlur)
  set('bg_offset', o.bgOffset)
  set('face_cap', o.faceCap)
  set('cost', o.cost)
  set('timeout', o.timeout)

  p['no_upscale'] = o.upscale !== true
  p['no_face'] = o.face !== true
  if (o.bgRefine) p['bg_refine'] = true

  return p
}
