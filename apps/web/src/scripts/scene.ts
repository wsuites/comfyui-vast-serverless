// Scene tab: the full pipeline from workflows/wf.json.

import type { Job, Options, SceneRequest, UploadedInput } from '../lib/types'
import { $, area, el, input, num, select } from './dom'
import { cancelJob, follow, submitScene, uploadInput } from './api'
import { JobView } from './progress'
import { markActive, refreshJobs } from './jobs'
import { zoomable } from './zoom'
import { initPose, posePayload, poseStrength, poseStyle, showPose } from './pose'

let view: JobView
let finished: (() => void) | undefined
let stream: EventSource | null = null
/** Id of the job this view is following, or null when nothing is running. */
let attached: string | null = null

function fillRemoveBg(models: string[]): void {
  select('remove_bg').replaceChildren(
    el('option', { value: '' }, 'none'),
    ...models.map((m) => el('option', { value: m }, m)),
  )
}

/**
 * The init image currently attached to the form, or null.
 *
 * The id, not the File: the bytes go up when the file is picked, so submitting
 * is a small JSON POST however large the image was, and re-rendering the same
 * start at another denoise does not upload it again.
 */
let init: UploadedInput | null = null

function fmtBytes(n: number): string {
  return n < 1024 ** 2 ? `${Math.round(n / 1024)} kB` : `${(n / 1024 ** 2).toFixed(1)} MB`
}

/** Reflect `init` into the preview, the status line and the denoise block. */
function showInit(): void {
  const preview = $<HTMLImageElement>('init-preview')
  $('init-placeholder').hidden = init !== null
  $<HTMLButtonElement>('init-clear').hidden = init === null
  preview.hidden = init === null
  $('denoise-cell').hidden = init === null
  $('init-status').hidden = init === null
  if (!init) {
    preview.removeAttribute('src')
    return
  }
  preview.src = `/api/inputs/${init.id}`
  const fitted = init.fit.width !== init.width || init.fit.height !== init.height
  $('init-status').textContent =
    `${init.width}×${init.height} · ${fmtBytes(init.bytes)}`
    + (fitted ? ` · resampled to ${init.fit.width}×${init.fit.height}, the SDXL-native box nearest this aspect` : '')
}

/**
 * Upload a picked file and adopt it as the start image.
 *
 * The size fields follow the image rather than staying at 1024×1024: a 16:9
 * photo sampled in a square latent comes back squashed, and that reads as a
 * model failure rather than a framing one. They stay editable - this sets
 * them, it does not lock them.
 */
async function takeFile(file: File): Promise<void> {
  const status = $('init-status')
  status.hidden = false
  status.textContent = `Uploading ${file.name}…`
  try {
    init = await uploadInput(file)
  } catch (e) {
    init = null
    showInit()
    status.hidden = false
    status.textContent = e instanceof Error ? e.message : String(e)
    return
  }
  input('width').value = String(init.fit.width)
  input('height').value = String(init.fit.height)
  showInit()
}

function clearInit(): void {
  init = null
  input('init_file').value = ''   // so the same file re-fires change
  showInit()
}

function initImageControls(options: Options): void {
  const drop = $('init-drop')
  const file = input('init_file')
  const denoise = input('denoise')

  if (options.init?.denoise_default !== undefined) {
    denoise.value = String(options.init.denoise_default)
  }
  const showDenoise = () => { $('denoise-out').textContent = Number(denoise.value).toFixed(2) }
  showDenoise()
  denoise.addEventListener('input', showDenoise)

  $<HTMLButtonElement>('init-pick').addEventListener('click', () => file.click())
  $<HTMLButtonElement>('init-clear').addEventListener('click', clearInit)
  file.addEventListener('change', () => {
    const picked = file.files?.[0]
    if (picked) void takeFile(picked)
  })

  // Drag and drop over the same target. dragover must be cancelled or the
  // browser navigates to the file instead of handing it over.
  for (const ev of ['dragenter', 'dragover'] as const) {
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over') })
  }
  for (const ev of ['dragleave', 'drop'] as const) {
    drop.addEventListener(ev, () => drop.classList.remove('over'))
  }
  drop.addEventListener('drop', (e) => {
    e.preventDefault()
    const dropped = (e as DragEvent).dataTransfer?.files?.[0]
    if (dropped) void takeFile(dropped)
  })
  showInit()
}

function body(): SceneRequest {
  const family = select('family').value
  return {
    prompt: area('prompt').value,
    negative: area('negative').value || null,
    seed: num(input('seed')),
    width: num(input('width')),
    height: num(input('height')),
    batch: num(input('batch')),
    steps: num(input('steps')),
    cfg: num(input('cfg')),
    family,
    init_image: init?.id ?? null,
    denoise: Number(input('denoise').value),
    lora: family === 'wai' ? num(input('lora')) : null,
    no_face: input('no_face').checked,
    detail_prompt: area('detail_prompt').value.trim() || null,
    detail_negative: area('detail_negative').value.trim() || null,
    no_upscale: input('no_upscale').checked,
    remove_bg: select('remove_bg').value || null,
    bg_refine: input('bg_refine').checked,
    bg_sensitivity: num(input('bg_sensitivity')) ?? 1.0,
    bg_blur: num(input('bg_blur')) ?? 0,
    bg_offset: num(input('bg_offset')) ?? 0,
    cost: num(input('cost')) ?? 100,
    timeout: num(input('timeout')) ?? 900,
    pose: posePayload(),
    pose_strength: poseStrength(),
    pose_style: poseStyle(),
  }
}

// The style LoRA is an SDXL file and the face pass is the part of the
// pipeline with the least evidence behind it on a DiT, so switching family
// rewrites what the form is allowed to ask for.
const FAMILY_NOTE: Record<string, string> = {
  wai: 'Checkpoint loader plus the style LoRA. Dropping the LoRA to 0 is the ' +
       'one lever measured to add texture (+46 % on bare text2img).',
  anima: 'Three separate loaders, Qwen3 text encoder, er_sde/simple on every ' +
         'sampling pass. The style LoRA is SDXL-only and does not apply. ' +
         'Face pass and upscale both verified against it (31 s bare, 23 s + ' +
         'face, 93 s + upscale). Measured no better than WAI on skin ' +
         'texture and about twice as slow per image.',
}

function showFamily(): void {
  const fam = select('family').value
  const wai = fam === 'wai'
  $('lora-cell').hidden = !wai
  $('family-note').textContent = FAMILY_NOTE[fam] ?? ''
  if (document.getElementById('pose-block')) showPose()
}

/** The job whose images the result panel is showing, for Retouch. */
let shown: Job | null = null

function showImages(job: Job): void {
  if (!job.images?.length) return
  shown = job
  $('scene-empty').hidden = true
  const p = job.params || {}
  $('scene-gallery').replaceChildren(...job.images.map((u, i) => el(
    'figure', {},
    el('img', { src: u, alt: `result ${i + 1}`, loading: 'lazy' }),
    el('figcaption', {},
      `${p.width}×${p.height} · seed ${p.seed ?? 'random'}`
      + (p.steps ? ` · ${p.steps} steps` : '')),
  )))
}

// The face pass is a no-op when it is not running, so the block that
// configures it follows the checkbox that turns it off.
function showFace(): void {
  $('face-tuning').hidden = input('no_face').checked
}

/**
 * Point the scene view at a job by id, whoever started it.
 *
 * A finished job still streams: the server sends one last full snapshot and
 * closes, so re-attaching to something that ended while the tab was shut
 * repaints its images instead of showing an empty panel.
 */
export function attachScene(jobId: string): void {
  stream?.close()                       // one EventSource at a time
  markActive(jobId)
  const go = $<HTMLButtonElement>('scene-go')
  const stop = $<HTMLButtonElement>('scene-cancel')
  go.disabled = true
  attached = jobId
  $('scene-gallery').replaceChildren()
  stream = follow(jobId, (job) => {
    view.update(job)
    showImages(job)
    // Only offer it while there is something to stop. Re-attaching to a job
    // that already ended must not show a live Cancel.
    stop.hidden = job.state !== 'running'
    stop.disabled = false
  }, (job) => {
    stream = null
    attached = null
    stop.hidden = true
    go.disabled = false
    if (job?.state === 'done') finished?.()
  })
}

export function initScene(options: Options, onFinished?: () => void): void {
  finished = onFinished
  fillRemoveBg(options.remove_bg)
  $('detail-default').textContent = options.detail?.prompt ?? ''
  area('detail_prompt').placeholder = options.detail?.prompt ?? ''
  area('detail_negative').placeholder = options.detail?.negative ?? ''
  initImageControls(options)
  initPose(options)
  showFamily()
  showFace()
  select('family').addEventListener('change', showFamily)
  input('no_face').addEventListener('change', showFace)
  view = new JobView($('scene-progress'))
  $<HTMLButtonElement>('scene-cancel').addEventListener('click', async (ev) => {
    const stop = ev.currentTarget as HTMLButtonElement
    if (attached === null) return
    // No confirm here: this button only exists while the job is on screen and
    // it is two clicks from having started it. The strip asks, because there
    // the click lands on a row the operator may not have been looking at.
    stop.disabled = true
    try {
      await cancelJob(attached)
    } catch (e) {
      stop.disabled = false
      view.fail(e instanceof Error ? e.message : String(e))
      return
    }
    refreshJobs()
  })
  zoomable($('scene-gallery'), (img) => img.closest('figure')?.textContent?.trim() ?? null,
    () => shown)

  $<HTMLFormElement>('scene-form').addEventListener('submit', async (ev) => {
    ev.preventDefault()
    const go = $<HTMLButtonElement>('scene-go')
    go.disabled = true
    $('scene-gallery').replaceChildren()

    let jobId: string
    try {
      ;({ job_id: jobId } = await submitScene(body()))
    } catch (e) {
      view.fail(e instanceof Error ? e.message : String(e))
      go.disabled = false
      return
    }
    attachScene(jobId)
    refreshJobs()                       // put it on the strip without waiting
  })
}
