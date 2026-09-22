// Scene tab: the full pipeline from workflows/wf.json.

import type { Job, Options, SceneRequest } from '../lib/types'
import { $, area, el, input, num, select } from './dom'
import { cancelJob, follow, submitScene } from './api'
import { JobView } from './progress'
import { markActive, refreshJobs } from './jobs'
import { zoomable } from './zoom'

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
}

function showImages(job: Job): void {
  if (!job.images?.length) return
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
  zoomable($('scene-gallery'), (img) => img.closest('figure')?.textContent?.trim() ?? null)

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
