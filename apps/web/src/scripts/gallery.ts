// Gallery tab: everything this page has generated, read back from
// output/web/index.jsonl so it survives a server restart.
//
// Compared pairs are grouped into one box: two arms, one seed, side by side,
// which is the whole point of having run them.

import type { Job } from '../lib/types'
import { $, el, fmt, input, select, when } from './dom'
import { getHistory } from './api'
import { zoomable } from './zoom'

let items: Job[] = []

type Line = [string, string]

function details(it: Job): Line[] {
  const p = it.params || {}
  const lines: Line[] = [
    ['id', it.id],
    ['when', when(it.created)],
    ['kind', it.kind === 'arm' ? `arm ${it.label}` : 'scene'],
    ['seed', String(p.seed ?? 'random')],
    ['size', `${p.width}×${p.height}`],
    ['steps', String(p.steps ?? 'arm default')],
    ['cfg', String(p.cfg ?? 'arm default')],
    ['worker', it.latency ? `${it.latency.toFixed(1)} s` : '—'],
    ['wall', fmt(it.elapsed)],
  ]
  if (it.kind !== 'arm') {
    lines.push(['model', p.family === 'anima' ? 'anima' : 'wai'])
    // Entries written before the family switch existed have no field at all,
    // which is not the same as a LoRA explicitly set to 0.
    if (p.family !== 'anima' && p.lora != null) lines.push(['lora', String(p.lora)])
    lines.push(['face', p.no_face ? 'skipped' : 'on'])
    // only worth a line when it was overridden; blank means the wf.json value
    if (!p.no_face && p.detail_prompt) lines.push(['face prompt', p.detail_prompt])
    lines.push(['upscale', p.no_upscale ? 'skipped' : 'on'])
    if (p.remove_bg) lines.push(['remove_bg', p.remove_bg])
  }
  lines.push(['prompt', p.prompt || ''])
  if (p.negative) lines.push(['negative', p.negative])
  if (it.error) lines.push(['error', it.error])
  return lines
}

function asText(it: Job): string {
  return details(it).map(([k, v]) => `${k}: ${v}`).join('\n')
}

function card(it: Job, url: string | null, index: number): HTMLElement {
  const p = it.params || {}
  const badges = [
    it.kind === 'arm'
      ? el('span', { class: 'badge arm' }, it.label)
      : el('span', { class: 'badge' }, 'scene'),
    it.state === 'error' ? el('span', { class: 'badge err' }, 'failed') : null,
  ]
  const fig = el('figure', { 'data-id': it.id },
    url !== null ? el('img', { src: url, alt: it.id, loading: 'lazy' })
                 : el('div', { class: 'empty', style: 'padding:24px' }, it.error || 'no image'),
    el('figcaption', {},
      el('div', {}, ...badges.filter(Boolean)),
      el('dl', { class: 'meta' },
        el('dt', {}, 'seed'), el('dd', {}, String(p.seed ?? 'random')),
        el('dt', {}, 'size'), el('dd', {}, `${p.width}×${p.height}`),
        el('dt', {}, 'worker'), el('dd', {}, it.latency ? `${it.latency.toFixed(1)} s` : '—'),
        el('dt', {}, 'when'), el('dd', {}, when(it.created)),
      )),
  )
  fig.dataset['index'] = String(index)
  return fig
}

function matches(it: Job): boolean {
  const kind = select('g_kind').value
  const arm = select('g_arm').value
  const q = input('g_search').value.trim().toLowerCase()
  if (kind && it.kind !== kind) return false
  if (arm && it.label !== arm) return false
  if (!q) return true
  const hay = `${it.params?.prompt ?? ''} ${it.params?.seed ?? ''} ${it.label ?? ''}`
  return hay.toLowerCase().includes(q)
}

interface Block {
  type: 'one' | 'pair'
  items: Job[]
}

function draw(): void {
  const shown = items.filter(matches)
  $('g_count').textContent = `${shown.length} of ${items.length}`
  $('g_empty').hidden = shown.length > 0

  // group the two sides of a compare under one heading
  const blocks: Block[] = []
  const pairs = new Map<string, Block>()
  for (const it of shown) {
    if (!it.pair) {
      blocks.push({ type: 'one', items: [it] })
      continue
    }
    let block = pairs.get(it.pair)
    if (block === undefined) {
      block = { type: 'pair', items: [] }
      pairs.set(it.pair, block)
      blocks.push(block)
    }
    block.items.push(it)
  }

  const out: HTMLElement[] = []
  const loose: Job[] = []
  for (const b of blocks) {
    if (b.type === 'one') {
      loose.push(...b.items)
      continue
    }
    if (loose.length) {
      out.push(el('div', { class: 'gallery' }, ...loose.map(cardsOf)))
      loose.length = 0
    }
    const sides = b.items.sort((x, y) => ((x.side ?? '') > (y.side ?? '') ? 1 : -1))
    const first = sides[0]
    out.push(el('div', { class: 'pairbox' },
      el('h3', {}, `A/B · ${sides.map((s) => s.label).join(' vs ')}`
        + ` · seed ${first?.params?.seed ?? '?'}`
        + ` · ${when(first?.created)}`),
      el('div', { class: 'gallery', style: 'margin-top:0' }, ...sides.map(cardsOf)),
    ))
  }
  if (loose.length) out.push(el('div', { class: 'gallery' }, ...loose.map(cardsOf)))
  $('g_items').replaceChildren(...out)
}

function cardsOf(it: Job): HTMLElement {
  const index = items.indexOf(it)
  if (!it.images?.length) return card(it, null, index)
  if (it.images.length === 1) return card(it, it.images[0] ?? null, index)
  return el('div', {}, ...it.images.map((u) => card(it, u, index)))
}

export async function refreshGallery(): Promise<void> {
  try {
    const { items: rows } = await getHistory()
    items = rows
  } catch (e) {
    $('g_empty').textContent =
      `Could not read the history: ${e instanceof Error ? e.message : String(e)}`
    items = []
  }
  const labels = items.map((i) => i.label).filter((l): l is string => Boolean(l))
  const arms = [...new Set(labels)].sort()
  const sel = select('g_arm')
  const keep = sel.value
  sel.replaceChildren(
    el('option', { value: '' }, 'Any arm'),
    ...arms.map((a) => el('option', { value: a }, a)),
  )
  sel.value = keep
  draw()
}

export function initGallery(): void {
  for (const id of ['g_kind', 'g_arm']) select(id).addEventListener('change', draw)
  input('g_search').addEventListener('input', draw)
  $('g_refresh').addEventListener('click', () => void refreshGallery())
  zoomable($('g_items'), (img) => {
    const fig = img.closest('figure')
    const index = Number(fig?.dataset['index'])
    const it = items[index]
    return it !== undefined ? asText(it) : null
  })
}
