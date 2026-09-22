// Compare tab: one prompt, two arms, one shared seed.
//
// The arms are the ones in scripts/ab_modelo.py, so a run here is directly
// comparable with the table in docs/levers-and-dead-ends.md - provided the
// steps/cfg overrides are left blank and each arm keeps its own config.

import type { ArmConfig, CompareRequest, Job, Options } from '../lib/types'
import { $, area, el, input, num, select } from './dom'
import { follow, submitCompare } from './api'
import { JobView } from './progress'
import { refreshJobs } from './jobs'
import { zoomable } from './zoom'

let ARMS: Record<string, ArmConfig> = {}

interface Side {
  view: JobView
  frame: HTMLDivElement
  arm: HTMLSpanElement
  tag: HTMLSpanElement
  caption?: string
}

const views: Record<string, Side> = {}

function sideOf(side: string): Side {
  const found = views[side]
  if (found === undefined) throw new Error(`mizuki console: side ${side} not built`)
  return found
}

function describeArm(name: string | null): string {
  const c = name === null ? undefined : ARMS[name]
  if (c === undefined) return ''
  // beta57 carries its schedule in a `beta57` pair instead of a `scheduler`
  // name, so nothing here may assume every key is present.
  const bits: (string | undefined)[] = [`family ${c.family}`]
  if (c.lora !== undefined) bits.push(`lora ${c.lora}`)
  bits.push(c.sampler)
  if (c.scheduler) bits.push(c.scheduler)
  if (c.beta57) bits.push(`beta ${c.beta57.join('/')}`)
  bits.push(`${c.steps} steps`, `cfg ${c.cfg}`)
  return bits.filter(Boolean).join(' · ')
}

function buildSide(side: string): void {
  const host = $(`side-${side}`)
  const arm = el('span', { class: 'arm' }, '—')
  const tag = el('span', { class: 'tag' })
  const frame = el('div', { class: 'frame' },
    el('div', { class: 'empty' }, 'waiting'))
  const progress = el('div')
  host.replaceChildren(
    el('h2', {}, `${side} · `, arm, tag),
    frame,
    progress,
  )
  views[side] = { view: new JobView(progress, { compact: true }), frame, arm, tag }
  zoomable(frame, () => sideOf(side).caption ?? null)
}

function paint(side: string, job: Job): void {
  const v = sideOf(side)
  v.arm.textContent = job.label || '—'
  v.view.update(job)
  const p = job.params || {}
  v.caption = [
    `arm ${job.label}`, `seed ${p.seed}`, `${p.width}×${p.height}`,
    describeArm(job.label), p.prompt,
  ].filter(Boolean).join('\n')

  if (job.images?.length) {
    v.frame.replaceChildren(el('img', { src: job.images[0], alt: `arm ${job.label}` }))
    v.tag.textContent = job.latency ? `${job.latency.toFixed(1)} s on the worker` : ''
  } else if (job.state === 'error') {
    v.frame.replaceChildren(el('div', { class: 'empty' }, 'failed'))
  }
}

function body(): CompareRequest {
  return {
    prompt: area('c_prompt').value,
    negative: area('c_negative').value || null,
    arm_a: select('c_arm_a').value,
    arm_b: select('c_arm_b').value,
    seed: num(input('c_seed')),
    width: num(input('c_width')),
    height: num(input('c_height')),
    steps: num(input('c_steps')),
    cfg: num(input('c_cfg')),
    anima_model: input('c_anima_model').value || null,
  }
}

function showConfigs(): void {
  $('c_cfg_a').textContent = `A · ${describeArm(select('c_arm_a').value)}`
  $('c_cfg_b').textContent = `B · ${describeArm(select('c_arm_b').value)}`
}

export function initCompare(options: Options, onFinished?: () => void): void {
  ARMS = options.arms
  const names = Object.keys(ARMS)
  const presets: [string, string | undefined][] = [
    ['c_arm_a', names[0]],
    ['c_arm_b', names.at(-1)],
  ]
  for (const [id, preset] of presets) {
    const sel = select(id)
    sel.replaceChildren(...names.map((n) => el('option', { value: n }, n)))
    if (preset !== undefined) sel.value = preset
    sel.addEventListener('change', showConfigs)
  }
  input('c_anima_model').value = options.anima_model
  buildSide('A')
  buildSide('B')
  showConfigs()

  $<HTMLFormElement>('compare-form').addEventListener('submit', async (ev) => {
    ev.preventDefault()
    const go = $<HTMLButtonElement>('compare-go')
    go.disabled = true
    $('compare-err').textContent = ''
    buildSide('A')
    buildSide('B')
    showConfigs()

    let out
    try {
      out = await submitCompare(body())
    } catch (e) {
      $('compare-err').textContent = e instanceof Error ? e.message : String(e)
      go.disabled = false
      return
    }
    input('c_seed').value = String(out.seed)   // pin it so the pair is repeatable
    refreshJobs()                              // both arms onto the strip now

    // Both streams open at once; the second job just sits in "queued" until
    // the first releases the worker.
    let left = out.jobs.length
    out.jobs.forEach((id, i) => {
      const side = i === 0 ? 'A' : 'B'
      sideOf(side).arm.textContent = out.arms[i] ?? '—'
      follow(id, (job) => paint(side, job), () => {
        if (--left === 0) {
          go.disabled = false
          onFinished?.()
        }
      })
    })
  })
}
