// Phase progress, shared by the scene tab and by each side of a compare.
//
// Why phases and not a percentage: the pyworker only exposes /generate/sync
// and /health, so there is no per-step callback and ComfyUI's own port is not
// published. Phases come from polling the Vast API for the worker's real
// state, which is where the time goes anyway - a cold start is ~7 min against
// a ~18 s render.

import type { Job } from '../lib/types'
import { el, fmt } from './dom'

export const PHASES = ['submitting', 'renting', 'booting', 'provisioning',
                       'generating', 'saving', 'done'] as const

export const PHASE_LABELS: Record<string, string> = {
  submitting: 'queued', renting: 'renting GPU', booting: 'booting image',
  provisioning: 'loading models', generating: 'rendering',
  saving: 'fetching image', done: 'done',
}

interface JobViewOptions {
  compact?: boolean
}

export class JobView {
  private readonly host: HTMLElement
  private readonly compact: boolean
  private readonly steps: HTMLDivElement
  private readonly detail: HTMLSpanElement
  private readonly clock: HTMLSpanElement
  private readonly err: HTMLDivElement
  private readonly log: HTMLDivElement

  /** @param host container this view owns and fills. */
  constructor(host: HTMLElement, { compact = false }: JobViewOptions = {}) {
    this.host = host
    this.compact = compact
    this.steps = el('div', { class: 'steps' })
    this.detail = el('span', { class: 'detail' })
    this.clock = el('span', { class: 'clock' })
    this.err = el('div', { class: 'err' })
    this.log = el('div', { class: 'log' })
    this.host.replaceChildren(
      this.steps,
      el('div', { class: 'status' }, this.detail, this.clock),
      this.err,
      ...(compact ? [] : [this.log]),
    )
    this.renderSteps(null, false)
  }

  private renderSteps(phase: string | null, failed: boolean): void {
    const at = phase === null ? -1 : (PHASES as readonly string[]).indexOf(phase)
    this.steps.replaceChildren(...PHASES.map((p, i) => {
      let cls = 'step'
      if (failed && i === at) cls += ' failed'
      else if (i === at) cls += ' active'
      else if (at > -1 && i < at) cls += ' past'
      return el('span', { class: cls }, PHASE_LABELS[p] ?? p)
    }))
  }

  update(job: Job): void {
    // A cancelled job stopped where it stopped: the step it died in is marked
    // like a failure, because it did not complete either.
    this.renderSteps(job.phase, job.state === 'error' || job.state === 'cancelled')
    this.detail.textContent = job.detail || ''
    this.clock.textContent =
      fmt(job.elapsed) + (job.latency ? ` · worker ${job.latency.toFixed(1)}s` : '')
    this.err.textContent = job.error ?? ''
    if (this.compact) return
    this.log.replaceChildren(...(job.log ?? []).map((e) => el(
      'div', {}, el('span', { class: 't' }, fmt(e.t)), el('span', {}, e.detail),
    )))
    this.log.scrollTop = this.log.scrollHeight
  }

  fail(message: string): void {
    this.err.textContent = message
  }
}
