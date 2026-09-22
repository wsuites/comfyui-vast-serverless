// The strip of jobs the server knows about.
//
// Why it exists: the page holds no state. It used to follow only the job whose
// id came back from its own submit, in memory, with nothing written down - so a
// reload, a second tab, or a `cv job submit` from the terminal left a render
// invisible while it was still burning GPU time. The server has always listed
// them at GET /api/jobs; this is the client half that was missing.
//
// Polled rather than streamed: /api/jobs is a plain list, and the per-job SSE
// route only follows one id. Two cadences, because a snapshot carries its whole
// log and there is no point pulling fifty of them every two seconds when the
// worker is idle.

import type { Job } from '../lib/types'
import { $, el, fmt } from './dom'
import { cancelJob, getJobs } from './api'
import { PHASE_LABELS } from './progress'

const BUSY_MS = 2000
const IDLE_MS = 15000
/** Finished jobs kept on the strip, so a render that just ended stays clickable. */
const KEEP_DONE = 6

let timer: number | undefined
let onPick: (jobId: string) => void = () => {}
let active: string | null = null

/** A job is identified by its prompt; the id is the fallback and the tooltip. */
function label(j: Job): string {
  const prompt = (j.params?.prompt ?? '').trim()
  if (prompt === '') return j.id
  return prompt.length > 42 ? prompt.slice(0, 42) + '…' : prompt
}

function badge(j: Job): string {
  if (j.kind === 'arm') return j.label ?? 'arm'
  return j.params?.family ?? j.kind
}

function status(j: Job): string {
  if (j.state === 'running') return PHASE_LABELS[j.phase] ?? j.phase
  if (j.state === 'error') return 'failed'
  if (j.state === 'cancelled') return 'cancelled'
  return `${j.images?.length ?? 0} img`
}

/**
 * Cancel one job from the strip.
 *
 * Confirmed first because there is no undo and the render is not free: the
 * worker keeps going either way, so cancelling by accident costs the GPU
 * minutes *and* the image. 409 means it finished between the poll and the
 * click, which is not an error worth shouting about - the next tick shows it.
 */
async function cancel(j: Job): Promise<void> {
  const what = j.kind === 'arm'
    ? `both arms of the compare (${j.label ?? j.id})`
    : label(j)
  if (!window.confirm(
    `Stop waiting for ${what}?

`
    + 'The worker cannot be interrupted: it will finish this render and the '
    + 'image will be discarded. This frees the console, not the GPU.',
  )) return
  try {
    await cancelJob(j.id)
  } catch (e) {
    if (!String(e).includes('409')) window.alert(`Could not cancel: ${String(e)}`)
  }
  void tick()
}

function card(j: Job): HTMLElement {
  // A wrapper rather than one button: the cancel control is interactive and
  // must not be nested inside the button that selects the job.
  const pick = el('button', {
    type: 'button',
    class: 'job-pick',
    title: j.params?.prompt ?? j.id,
    onclick: () => {
      active = j.id
      onPick(j.id)
      void tick()                       // move the highlight without waiting
    },
  },
    el('span', { class: 'job-dot' }),
    el('span', { class: 'job-name' }, label(j)),
    el('span', { class: 'job-arm' }, badge(j)),
    el('span', { class: 'job-phase' }, status(j)),
    el('span', { class: 'job-clock' }, fmt(j.elapsed)),
  )

  return el('span', {
    class: `job job-${j.state}` + (j.id === active ? ' job-active' : ''),
  },
    pick,
    j.state === 'running' && el('button', {
      type: 'button',
      class: 'job-cancel',
      title: 'Cancel this job',
      'aria-label': `Cancel ${label(j)}`,
      onclick: (ev: Event) => {
        ev.stopPropagation()
        void cancel(j)
      },
    }, '×'),
  )
}

function render(jobs: Job[]): void {
  const running = jobs.filter((j) => j.state === 'running')
  const rest = jobs.filter((j) => j.state !== 'running').slice(0, KEEP_DONE)
  const list = [...running, ...rest]

  $('active-jobs').hidden = list.length === 0
  $('active-jobs-count').textContent = running.length === 0
    ? 'nothing running'
    : `${running.length} running`
  $('active-jobs-list').replaceChildren(...list.map(card))
}

function schedule(ms: number): void {
  clearTimeout(timer)
  timer = window.setTimeout(() => void tick(), ms)
}

async function tick(): Promise<void> {
  let jobs: Job[]
  try {
    ;({ jobs } = await getJobs())
  } catch {
    // The strip is a convenience, not the render path: a failed poll leaves the
    // last good list on screen and retries on the slow cadence.
    schedule(IDLE_MS)
    return
  }
  render(jobs)
  schedule(jobs.some((j) => j.state === 'running') ? BUSY_MS : IDLE_MS)
}

/** Pull the list now - called right after a submit, which cannot wait 15 s. */
export function refreshJobs(): void {
  void tick()
}

/** Mark which job the scene view is currently attached to. */
export function markActive(jobId: string): void {
  active = jobId
}

export function initJobs(pick: (jobId: string) => void): void {
  onPick = pick
  void tick()
  // A hidden tab does not need to poll; catching up when it comes back is enough.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') void tick()
    else clearTimeout(timer)
  })
}
