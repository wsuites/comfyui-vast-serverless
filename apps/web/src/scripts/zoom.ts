// One modal for the whole page: click any image to open it full size, with
// the parameters that produced it underneath.

import type { Job } from '../lib/types'
import { $, el } from './dom'

let dialog: HTMLDialogElement | null = null
let img: HTMLImageElement
let info: HTMLDivElement
let bar: HTMLDivElement
let current: { src: string; job: Job | null } | null = null
let retouch: ((src: string, job: Job | null) => void) | null = null

/** Offer a Retouch button on every zoomed image, handing it to `fn`. */
export function setRetouch(fn: (src: string, job: Job | null) => void): void {
  retouch = fn
}

function ensure(): HTMLDialogElement {
  if (dialog !== null) return dialog
  const node = $<HTMLDialogElement>('zoom')
  const picture = node.querySelector('img')
  if (picture === null) throw new Error('autoscaler-vast console: #zoom has no <img>')
  img = picture
  info = el('div', { class: 'zoominfo', hidden: true })
  const button = el('button', {
    type: 'button',
    title: 'Paint over part of this image and regenerate only that area',
    onclick: () => {
      if (current === null || retouch === null) return
      const { src, job } = current
      node.close()
      retouch(src, job)
    },
  }, 'Retouch')
  bar = el('div', { class: 'zoombar' }, button)
  node.append(bar, info)
  node.addEventListener('click', (ev) => {
    const target = ev.target
    // Clicking the caption or the toolbar should not dismiss the image.
    const inside = (box: HTMLElement): boolean => target instanceof Node && box.contains(target)
    if (!inside(info) && !inside(bar)) node.close()
  })
  dialog = node
  return node
}

export function openZoom(src: string, details: string | null = null, job: Job | null = null): void {
  const node = ensure()
  img.src = src
  current = { src, job }
  bar.hidden = retouch === null
  if (details !== null && details !== '') {
    info.textContent = details
    info.hidden = false
  } else {
    info.hidden = true
  }
  node.showModal()
}

/** Delegate clicks on images inside `root` to the modal. */
export function zoomable(
  root: HTMLElement,
  describe: (img: HTMLImageElement) => string | null = () => null,
  source: (img: HTMLImageElement) => Job | null = () => null,
): void {
  root.addEventListener('click', (ev) => {
    const target = ev.target
    if (!(target instanceof HTMLImageElement)) return
    openZoom(target.src, describe(target), source(target))
  })
}
