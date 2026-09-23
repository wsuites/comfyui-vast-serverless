// One modal for the whole page: click any image to open it full size, with
// the parameters that produced it underneath.

import { $, el } from './dom'

let dialog: HTMLDialogElement | null = null
let img: HTMLImageElement
let info: HTMLDivElement

function ensure(): HTMLDialogElement {
  if (dialog !== null) return dialog
  const node = $<HTMLDialogElement>('zoom')
  const picture = node.querySelector('img')
  if (picture === null) throw new Error('autoscaler-vast console: #zoom has no <img>')
  img = picture
  info = el('div', { class: 'zoominfo', hidden: true })
  node.append(info)
  node.addEventListener('click', (ev) => {
    const target = ev.target
    // Clicking the caption should not dismiss what it is describing.
    if (target !== info && !(target instanceof Node && info.contains(target))) node.close()
  })
  dialog = node
  return node
}

export function openZoom(src: string, details: string | null = null): void {
  const node = ensure()
  img.src = src
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
): void {
  root.addEventListener('click', (ev) => {
    const target = ev.target
    if (!(target instanceof HTMLImageElement)) return
    openZoom(target.src, describe(target))
  })
}
