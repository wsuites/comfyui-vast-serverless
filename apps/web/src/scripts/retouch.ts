// Retouch editor: paint a mask over a finished image and send only that region
// back through the sampler.
//
// Two tools feed one mask. The brush (and its eraser) paints into an offscreen
// canvas at the image's own resolution. Rectangles are kept as geometry, so
// they stay editable by their four corners until the mask is sent. The mask
// that goes to the server is their union, white on black, the same size as
// the image; the server crops around its bounding box (see _inpaint_crop).

import type { Job, MaskBox, Options, SceneRequest } from '../lib/types'
import { $, area, input, select } from './dom'
import { submitScene, uploadInput } from './api'

type Tool = 'brush' | 'eraser' | 'rect'

/** Natural-pixel rectangle; always normalised to a positive width/height. */
interface Rect { x: number; y: number; w: number; h: number }

interface Snapshot { brush: ImageData; rects: Rect[] }

type Drag =
  | { kind: 'paint'; last: { x: number; y: number } }
  | { kind: 'corner'; index: number; ax: number; ay: number }
  | { kind: 'move'; index: number; dx: number; dy: number; start: Rect }

const HANDLE = 7            // corner handle half-size, in screen px
const HISTORY_BYTES = 128 * 1024 * 1024
const MIN_RECT = 8          // natural px; anything smaller was a click, not a box
const FILL = 'rgba(255, 60, 60, 0.45)'

let dialog: HTMLDialogElement
let image: HTMLImageElement
let overlay: HTMLCanvasElement
let brush: HTMLCanvasElement = document.createElement('canvas')
let rects: Rect[] = []
let selected = -1
let tool: Tool = 'brush'
let drag: Drag | null = null
let before: Snapshot | null = null
let history: Snapshot[] = []
let pointer: { x: number; y: number } | null = null  // screen px inside the overlay
let source: { src: string; job: Job | null } | null = null
let submitted: (jobId: string) => void = () => {}
let denoiseDefault = 0.6

const natW = (): number => image.naturalWidth
const natH = (): number => image.naturalHeight

/** CSS px on screen per natural image px. */
function scale(): number {
  const w = overlay.getBoundingClientRect().width
  return w > 0 && natW() > 0 ? w / natW() : 1
}

function toNatural(ev: PointerEvent): { x: number; y: number } {
  const r = overlay.getBoundingClientRect()
  const s = scale()
  return { x: (ev.clientX - r.left) / s, y: (ev.clientY - r.top) / s }
}

function brushSize(): number {
  return Number(input('rt-size').value) / scale()   // slider is in screen px
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, v))
}

function hasMask(): boolean {
  return rects.length > 0 || history.length > 0
}

// --- history --------------------------------------------------------------

function snapshot(): Snapshot {
  const ctx = brush.getContext('2d')!
  return { brush: ctx.getImageData(0, 0, brush.width, brush.height), rects: rects.map((r) => ({ ...r })) }
}

function commit(snap: Snapshot): void {
  history.push(snap)
  // Each entry is a full-resolution RGBA copy of the brush layer; keep as many
  // as fit the budget, so an upscaled 2432x1664 image keeps fewer steps.
  const perEntry = Math.max(1, brush.width * brush.height * 4)
  const keep = Math.max(3, Math.floor(HISTORY_BYTES / perEntry))
  if (history.length > keep) history.splice(0, history.length - keep)
}

function undo(): void {
  const snap = history.pop()
  if (snap === undefined) return
  brush.getContext('2d')!.putImageData(snap.brush, 0, 0)
  rects = snap.rects
  selected = -1
  render()
}

function clearAll(): void {
  commit(snapshot())
  brush.getContext('2d')!.clearRect(0, 0, brush.width, brush.height)
  rects = []
  selected = -1
  render()
}

// --- drawing --------------------------------------------------------------

function strokeTo(from: { x: number; y: number }, to: { x: number; y: number }): void {
  const ctx = brush.getContext('2d')!
  ctx.save()
  ctx.globalCompositeOperation = tool === 'eraser' ? 'destination-out' : 'source-over'
  ctx.strokeStyle = ctx.fillStyle = '#ff3c3c'
  ctx.lineWidth = brushSize()
  ctx.lineCap = ctx.lineJoin = 'round'
  if (from.x === to.x && from.y === to.y) {
    ctx.beginPath()
    ctx.arc(to.x, to.y, ctx.lineWidth / 2, 0, Math.PI * 2)
    ctx.fill()
  } else {
    ctx.beginPath()
    ctx.moveTo(from.x, from.y)
    ctx.lineTo(to.x, to.y)
    ctx.stroke()
  }
  ctx.restore()
}

function corners(r: Rect): [number, number][] {
  return [[r.x, r.y], [r.x + r.w, r.y], [r.x, r.y + r.h], [r.x + r.w, r.y + r.h]]
}

function render(): void {
  const r = overlay.getBoundingClientRect()
  const dpr = window.devicePixelRatio || 1
  const w = Math.max(1, Math.round(r.width * dpr))
  const h = Math.max(1, Math.round(r.height * dpr))
  if (overlay.width !== w || overlay.height !== h) {
    overlay.width = w
    overlay.height = h
  }
  const ctx = overlay.getContext('2d')!
  ctx.setTransform(1, 0, 0, 1, 0, 0)
  ctx.clearRect(0, 0, w, h)
  const k = scale() * dpr   // device px per natural px

  ctx.globalAlpha = 0.45
  ctx.drawImage(brush, 0, 0, w, h)
  ctx.globalAlpha = 1

  rects.forEach((rc, i) => {
    ctx.fillStyle = FILL
    ctx.fillRect(rc.x * k, rc.y * k, rc.w * k, rc.h * k)
    ctx.lineWidth = dpr
    ctx.strokeStyle = i === selected ? '#ffffff' : 'rgba(255,255,255,.55)'
    ctx.setLineDash(i === selected ? [] : [4 * dpr, 3 * dpr])
    ctx.strokeRect(rc.x * k, rc.y * k, rc.w * k, rc.h * k)
    ctx.setLineDash([])
    if (i === selected && tool === 'rect') {
      const hs = HANDLE * dpr
      for (const [cx, cy] of corners(rc)) {
        ctx.fillStyle = '#ffffff'
        ctx.fillRect(cx * k - hs, cy * k - hs, hs * 2, hs * 2)
        ctx.strokeStyle = '#000000'
        ctx.strokeRect(cx * k - hs, cy * k - hs, hs * 2, hs * 2)
      }
    }
  })

  // Brush outline under the pointer, at the size it will actually paint.
  if (pointer !== null && tool !== 'rect') {
    ctx.beginPath()
    ctx.arc(pointer.x * dpr, pointer.y * dpr, (Number(input('rt-size').value) / 2) * dpr, 0, Math.PI * 2)
    ctx.lineWidth = dpr
    ctx.strokeStyle = tool === 'eraser' ? '#7aa2f7' : '#ffffff'
    ctx.stroke()
  }

  $<HTMLButtonElement>('rt-delete').disabled = selected < 0
}

// --- pointer handling -----------------------------------------------------

/** Corner of the selected rect under a screen point, as the opposite corner. */
function hitCorner(p: { x: number; y: number }): { ax: number; ay: number } | null {
  const rc = rects[selected]
  if (rc === undefined) return null
  const s = scale()
  const tol = (HANDLE + 3) / s
  for (const [cx, cy] of corners(rc)) {
    if (Math.abs(p.x - cx) <= tol && Math.abs(p.y - cy) <= tol) {
      return { ax: cx === rc.x ? rc.x + rc.w : rc.x, ay: cy === rc.y ? rc.y + rc.h : rc.y }
    }
  }
  return null
}

function hitRect(p: { x: number; y: number }): number {
  for (let i = rects.length - 1; i >= 0; i--) {
    const rc = rects[i]!
    if (p.x >= rc.x && p.x <= rc.x + rc.w && p.y >= rc.y && p.y <= rc.y + rc.h) return i
  }
  return -1
}

function onDown(ev: PointerEvent): void {
  if (ev.button !== 0 || natW() === 0) return
  overlay.setPointerCapture(ev.pointerId)
  const p = toNatural(ev)
  before = snapshot()
  if (tool !== 'rect') {
    drag = { kind: 'paint', last: p }
    strokeTo(p, p)
  } else {
    const corner = hitCorner(p)
    const inside = hitRect(p)
    if (corner !== null) {
      drag = { kind: 'corner', index: selected, ...corner }
    } else if (inside >= 0) {
      selected = inside
      const rc = rects[inside]!
      drag = { kind: 'move', index: inside, dx: p.x - rc.x, dy: p.y - rc.y, start: { ...rc } }
    } else {
      rects.push({ x: clamp(p.x, 0, natW()), y: clamp(p.y, 0, natH()), w: 0, h: 0 })
      selected = rects.length - 1
      drag = { kind: 'corner', index: selected, ax: clamp(p.x, 0, natW()), ay: clamp(p.y, 0, natH()) }
    }
  }
  render()
}

function onMove(ev: PointerEvent): void {
  const r = overlay.getBoundingClientRect()
  pointer = { x: ev.clientX - r.left, y: ev.clientY - r.top }
  if (drag !== null) {
    const p = toNatural(ev)
    if (drag.kind === 'paint') {
      strokeTo(drag.last, p)
      drag.last = p
    } else if (drag.kind === 'corner') {
      const x = clamp(p.x, 0, natW())
      const y = clamp(p.y, 0, natH())
      rects[drag.index] = {
        x: Math.min(drag.ax, x), y: Math.min(drag.ay, y),
        w: Math.abs(x - drag.ax), h: Math.abs(y - drag.ay),
      }
    } else {
      const s = drag.start
      rects[drag.index] = {
        ...s,
        x: clamp(p.x - drag.dx, 0, natW() - s.w),
        y: clamp(p.y - drag.dy, 0, natH() - s.h),
      }
    }
  }
  render()
}

function onUp(): void {
  if (drag === null) return
  const wasNew = drag.kind === 'corner'
  drag = null
  // A click on empty space in rectangle mode draws a zero-size box: drop it,
  // and drop any box squeezed below a usable size by its corners.
  if (wasNew) {
    const rc = rects[selected]
    if (rc !== undefined && (rc.w < MIN_RECT || rc.h < MIN_RECT)) {
      rects.splice(selected, 1)
      selected = -1
    }
  }
  const snap = before
  before = null
  if (snap !== null && (tool !== 'rect' || JSON.stringify(snap.rects) !== JSON.stringify(rects))) {
    commit(snap)
  }
  render()
}

function deleteSelected(): void {
  if (selected < 0) return
  commit(snapshot())
  rects.splice(selected, 1)
  selected = -1
  render()
}

function setTool(next: Tool): void {
  tool = next
  for (const b of dialog.querySelectorAll<HTMLButtonElement>('.rt-tool')) {
    b.classList.toggle('on', b.dataset['tool'] === next)
  }
  overlay.style.cursor = next === 'rect' ? 'crosshair' : 'none'
  render()
}

// --- the mask that goes to the server ------------------------------------

/** White-on-black mask at the image's size, plus the painted bounding box. */
function exportMask(): { canvas: HTMLCanvasElement; box: MaskBox | null } {
  const w = natW()
  const h = natH()
  const out = document.createElement('canvas')
  out.width = w
  out.height = h
  const ctx = out.getContext('2d')!
  ctx.fillStyle = '#000'
  ctx.fillRect(0, 0, w, h)

  // Brush strokes become white wherever they have any alpha.
  const white = document.createElement('canvas')
  white.width = w
  white.height = h
  const wctx = white.getContext('2d')!
  wctx.drawImage(brush, 0, 0)
  wctx.globalCompositeOperation = 'source-in'
  wctx.fillStyle = '#fff'
  wctx.fillRect(0, 0, w, h)
  ctx.drawImage(white, 0, 0)

  ctx.fillStyle = '#fff'
  for (const rc of rects) ctx.fillRect(Math.round(rc.x), Math.round(rc.y), Math.round(rc.w), Math.round(rc.h))

  const data = ctx.getImageData(0, 0, w, h).data
  let x0 = w, y0 = h, x1 = -1, y1 = -1
  for (let y = 0; y < h; y++) {
    const row = y * w * 4
    for (let x = 0; x < w; x++) {
      if (data[row + x * 4]! > 0) {
        if (x < x0) x0 = x
        if (x > x1) x1 = x
        if (y < y0) y0 = y
        if (y > y1) y1 = y
      }
    }
  }
  const box = x1 < 0 ? null : { x: x0, y: y0, width: x1 - x0 + 1, height: y1 - y0 + 1 }
  return { canvas: out, box }
}

function toFile(canvas: HTMLCanvasElement, name: string): Promise<File> {
  return new Promise((resolve, reject) => {
    canvas.toBlob((b) => (b === null ? reject(new Error('could not encode the mask'))
      : resolve(new File([b], name, { type: 'image/png' }))), 'image/png')
  })
}

function status(text: string | null): void {
  const node = $('rt-status')
  node.hidden = text === null
  node.textContent = text ?? ''
}

async function submit(ev: SubmitEvent): Promise<void> {
  ev.preventDefault()
  if (source === null) return
  const { canvas, box } = exportMask()
  if (box === null) {
    status('Paint or draw a rectangle over the area to redo first.')
    return
  }
  const go = $<HTMLButtonElement>('rt-go')
  go.disabled = true
  try {
    status('Uploading the image…')
    const res = await fetch(source.src)
    if (!res.ok) throw new Error(`could not read the image back (${res.status})`)
    const blob = await res.blob()
    const init = await uploadInput(new File([blob], 'retouch-source', { type: blob.type || 'image/png' }))
    status('Uploading the mask…')
    const mask = await uploadInput(await toFile(canvas, 'retouch-mask.png'))
    if (init.width !== mask.width || init.height !== mask.height) {
      throw new Error(`mask is ${mask.width}×${mask.height} but the image is ${init.width}×${init.height}`)
    }
    const p = source.job?.params ?? {}
    const family = select('rt-family').value
    const body: SceneRequest = {
      prompt: area('rt-prompt').value,
      negative: area('rt-negative').value.trim() || null,
      seed: null,
      width: null,            // the server sizes the sample to the crop
      height: null,
      batch: Number(input('rt-batch').value) || 1,
      steps: p.steps ?? null,
      cfg: p.cfg ?? null,
      family,
      init_image: init.id,
      denoise: Number(input('rt-denoise').value),
      lora: family === 'wai' ? (p.lora ?? null) : null,
      no_face: input('rt-no-face').checked,
      detail_prompt: p.detail_prompt ?? null,
      detail_negative: p.detail_negative ?? null,
      // The patch is scaled back to the crop's size, so upscaling it first
      // only costs time.
      no_upscale: true,
      remove_bg: null,
      bg_refine: false,
      bg_sensitivity: 1,
      bg_blur: 0,
      bg_offset: 0,
      cost: 100,
      timeout: 900,
      mask_image: mask.id,
      mask_box: box,
      mask_pad: Number(input('rt-pad').value) || 0,
      mask_blur: Number(input('rt-blur').value) || 0,
    }
    status('Queuing the job…')
    const { job_id: jobId } = await submitScene(body)
    status(null)
    dialog.close()
    submitted(jobId)
  } catch (e) {
    status(e instanceof Error ? e.message : String(e))
  } finally {
    go.disabled = false
  }
}

// --- public ---------------------------------------------------------------

/** Open the editor on an image; `job` supplies prompt and model defaults. */
export function openRetouch(src: string, job: Job | null): void {
  source = { src, job }
  const p = job?.params ?? {}
  area('rt-prompt').value = p.prompt ?? ''
  area('rt-negative').value = p.negative ?? ''
  select('rt-family').value = p.family === 'anima' ? 'anima' : 'wai'
  input('rt-denoise').value = String(denoiseDefault)
  $('rt-denoise-out').textContent = denoiseDefault.toFixed(2)
  status(null)
  rects = []
  selected = -1
  history = []
  $('rt-dims').textContent = 'Loading…'

  image.onload = () => {
    brush = document.createElement('canvas')
    brush.width = natW()
    brush.height = natH()
    $('rt-dims').textContent = `${natW()}×${natH()} · brush and rectangles add up to one mask; `
      + 'the eraser only removes brush strokes.'
    render()
  }
  image.onerror = () => { $('rt-dims').textContent = 'Could not load this image.' }
  image.src = src
  setTool('brush')
  dialog.showModal()
}

export function initRetouch(options: Options, onSubmitted: (jobId: string) => void): void {
  dialog = $<HTMLDialogElement>('retouch')
  image = $<HTMLImageElement>('rt-image')
  overlay = $<HTMLCanvasElement>('rt-overlay')
  submitted = onSubmitted
  denoiseDefault = options.init?.denoise_default ?? 0.6

  overlay.addEventListener('pointerdown', onDown)
  overlay.addEventListener('pointermove', onMove)
  overlay.addEventListener('pointerup', onUp)
  overlay.addEventListener('pointercancel', onUp)
  overlay.addEventListener('pointerleave', () => { pointer = null; render() })

  for (const b of dialog.querySelectorAll<HTMLButtonElement>('.rt-tool')) {
    b.addEventListener('click', () => setTool(b.dataset['tool'] as Tool))
  }
  input('rt-size').addEventListener('input', () => {
    $('rt-size-out').textContent = input('rt-size').value
    render()
  })
  input('rt-denoise').addEventListener('input', () => {
    $('rt-denoise-out').textContent = Number(input('rt-denoise').value).toFixed(2)
  })
  $('rt-undo').addEventListener('click', undo)
  $('rt-clear').addEventListener('click', clearAll)
  $('rt-delete').addEventListener('click', deleteSelected)
  $('rt-cancel').addEventListener('click', () => dialog.close())
  $<HTMLFormElement>('rt-form').addEventListener('submit', (ev) => void submit(ev))

  // Esc would throw the mask away; only the Cancel button may do that once
  // something is painted.
  dialog.addEventListener('cancel', (ev) => { if (hasMask()) ev.preventDefault() })

  window.addEventListener('resize', () => { if (dialog.open) render() })
  document.addEventListener('keydown', (ev) => {
    if (!dialog.open) return
    const typing = ev.target instanceof HTMLTextAreaElement
      || (ev.target instanceof HTMLInputElement && ev.target.type !== 'range' && ev.target.type !== 'checkbox')
    if (typing) return
    const key = ev.key.toLowerCase()
    if ((ev.ctrlKey || ev.metaKey) && key === 'z') { ev.preventDefault(); undo() }
    else if (key === 'delete' || key === 'backspace') { ev.preventDefault(); deleteSelected() }
    else if (key === 'b') setTool('brush')
    else if (key === 'e') setTool('eraser')
    else if (key === 'r') setTool('rect')
    else if (key === '[' || key === ']') {
      const s = input('rt-size')
      s.value = String(Number(s.value) + (key === ']' ? 5 : -5))
      $('rt-size-out').textContent = s.value
      render()
    }
  })
}
