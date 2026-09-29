// Skeleton editor for Anima pose control: 17 COCO body joints dragged over a
// canvas with the scene's aspect. The server renders them into the control
// image (AnimaPoseControl, pose_json), so what goes up is just coordinates.

import type { Options, PoseSpec } from '../lib/types'
import { $, el, input, num, select } from './dom'

/** COCO-17 order. Subject's left is image right. */
const NAMES = [
  'nose', 'left eye', 'right eye', 'left ear', 'right ear',
  'left shoulder', 'right shoulder', 'left elbow', 'right elbow',
  'left wrist', 'right wrist', 'left hip', 'right hip',
  'left knee', 'right knee', 'left ankle', 'right ankle',
]

const LIMBS: [number, number, string][] = [
  [0, 1, '#ff55ff'], [0, 2, '#aa55ff'], [1, 3, '#ff55ff'], [2, 4, '#aa55ff'],
  [5, 6, '#ffaa00'], [5, 7, '#ffff00'], [7, 9, '#aaff00'], [6, 8, '#ff5500'], [8, 10, '#ff0000'],
  [5, 11, '#00ffaa'], [6, 12, '#00ff55'], [11, 12, '#00aaff'],
  [11, 13, '#00ffff'], [13, 15, '#00aaff'], [12, 14, '#55ff00'], [14, 16, '#00ff00'],
]

/**
 * A joint: x as an offset from the frame's centre, y from the top, both in
 * units of the frame's height. Keeping one unit on both axes means changing
 * width or height reframes the figure instead of stretching it.
 */
interface Joint { x: number; y: number; on: boolean }

type Preset = [number, number][]

const HEAD: Preset = [[0, 0.14], [0.02, 0.125], [-0.02, 0.125], [0.045, 0.135], [-0.045, 0.135]]
const HIPS_LEGS: Preset = [[0.055, 0.54], [-0.055, 0.54], [0.06, 0.72], [-0.06, 0.72], [0.065, 0.9], [-0.065, 0.9]]

// Arms are measured to follow up to ~60° above horizontal; straight up (90°)
// comes back as a back view in every style, so no preset asks for it.
const PRESETS: Record<string, Preset> = {
  standing: [...HEAD, [0.08, 0.24], [-0.08, 0.24], [0.1, 0.4], [-0.1, 0.4], [0.11, 0.54], [-0.11, 0.54], ...HIPS_LEGS],
  'arms out': [...HEAD, [0.08, 0.24], [-0.08, 0.24], [0.2, 0.25], [-0.2, 0.25], [0.32, 0.26], [-0.32, 0.26], ...HIPS_LEGS],
  wave: [...HEAD, [0.08, 0.24], [-0.08, 0.24], [0.16, 0.38], [-0.18, 0.17], [0.08, 0.52], [-0.24, 0.07], ...HIPS_LEGS],
  'hands on hips': [...HEAD, [0.08, 0.24], [-0.08, 0.24], [0.17, 0.37], [-0.17, 0.37], [0.07, 0.52], [-0.07, 0.52], ...HIPS_LEGS],
  walking: [...HEAD, [0.08, 0.24], [-0.08, 0.24], [0.13, 0.39], [-0.05, 0.39], [0.16, 0.52], [-0.02, 0.53],
    [0.055, 0.54], [-0.055, 0.54], [0.1, 0.71], [-0.07, 0.72], [0.14, 0.89], [-0.12, 0.9]],
}

let joints: Joint[] = []
let dragging = -1
let hover = -1

const canvas = (): HTMLCanvasElement => $<HTMLCanvasElement>('pose-canvas')

/** The scene's size, falling back to the inputs' defaults while one is blank. */
function frame(): { w: number; h: number } {
  return { w: num(input('width')) || 1024, h: num(input('height')) || 1024 }
}

function load(name: string): void {
  joints = (PRESETS[name] ?? PRESETS.standing!).map(([x, y]) => ({ x, y, on: true }))
  draw()
}

function toPx(j: Joint, w: number, h: number): [number, number] {
  return [w / 2 + j.x * h, j.y * h]
}

function draw(): void {
  const c = canvas()
  const { w, h } = frame()
  // Backing store at a fixed height so joints stay the same size on screen
  // whatever the render size is; the CSS scales it into the column.
  const H = 480
  const W = Math.max(1, Math.round((H * w) / h))
  if (c.width !== W || c.height !== H) { c.width = W; c.height = H }
  const ctx = c.getContext('2d')!
  ctx.fillStyle = '#0b0d12'
  ctx.fillRect(0, 0, W, H)
  const pts = joints.map((j) => toPx(j, W, H))
  ctx.lineWidth = 6
  ctx.lineCap = 'round'
  for (const [a, b, color] of LIMBS) {
    const pa = pts[a]
    const pb = pts[b]
    if (!pa || !pb || !joints[a]?.on || !joints[b]?.on) continue
    ctx.strokeStyle = color
    ctx.globalAlpha = 0.75
    ctx.beginPath()
    ctx.moveTo(pa[0], pa[1])
    ctx.lineTo(pb[0], pb[1])
    ctx.stroke()
  }
  ctx.globalAlpha = 1
  pts.forEach(([x, y], i) => {
    ctx.beginPath()
    ctx.arc(x, y, i === hover || i === dragging ? 8 : 6, 0, Math.PI * 2)
    if (joints[i]?.on) {
      ctx.fillStyle = '#e6e9ef'
      ctx.fill()
    } else {
      ctx.strokeStyle = '#8b93a3'
      ctx.lineWidth = 2
      ctx.stroke()
    }
  })
  $('pose-hint').textContent = hover >= 0
    ? `${NAMES[hover]}${joints[hover]?.on ? '' : ' (hidden)'}`
    : 'Drag a joint to move it; double-click to hide or show it.'
}

/** Pointer position in backing-store pixels. */
function at(ev: PointerEvent | MouseEvent): [number, number] {
  const c = canvas()
  const r = c.getBoundingClientRect()
  return [((ev.clientX - r.left) * c.width) / r.width, ((ev.clientY - r.top) * c.height) / r.height]
}

function nearest(ev: PointerEvent | MouseEvent): number {
  const c = canvas()
  const [px, py] = at(ev)
  let best = -1
  let bestD = 14 ** 2
  joints.forEach((j, i) => {
    const [x, y] = toPx(j, c.width, c.height)
    const d = (x - px) ** 2 + (y - py) ** 2
    if (d < bestD) { bestD = d; best = i }
  })
  return best
}

function bindCanvas(): void {
  const c = canvas()
  c.addEventListener('pointerdown', (ev) => {
    dragging = nearest(ev)
    if (dragging >= 0) { c.setPointerCapture(ev.pointerId); ev.preventDefault() }
  })
  c.addEventListener('pointermove', (ev) => {
    if (dragging < 0) {
      const h = nearest(ev)
      if (h !== hover) { hover = h; draw() }
      c.style.cursor = h >= 0 ? 'grab' : 'default'
      return
    }
    const [px, py] = at(ev)
    const x = Math.min(Math.max(px, 0), c.width)
    const y = Math.min(Math.max(py, 0), c.height)
    const j = joints[dragging]
    if (j) joints[dragging] = { on: j.on, x: (x - c.width / 2) / c.height, y: y / c.height }
    draw()
  })
  const stop = (): void => { dragging = -1; draw() }
  c.addEventListener('pointerup', stop)
  c.addEventListener('pointercancel', stop)
  c.addEventListener('pointerleave', () => { if (dragging < 0 && hover >= 0) { hover = -1; draw() } })
  c.addEventListener('dblclick', (ev) => {
    const i = nearest(ev)
    const j = joints[i]
    if (!j) return
    joints[i] = { ...j, on: !j.on }
    draw()
  })
}

/** Whether the pose block applies: Anima only, and switched on. */
export function poseActive(): boolean {
  return select('family').value === 'anima' && input('pose_on').checked
}

/**
 * Joints in scene pixels, [x, y, score]. A joint dragged or reframed out of
 * the picture is sent hidden rather than clamped onto the edge, where it
 * would pull a limb that is meant to be out of shot.
 */
export function posePayload(): PoseSpec | null {
  if (!poseActive()) return null
  const { w, h } = frame()
  const points = joints.map((j): [number, number, number] => {
    const [x, y] = toPx(j, w, h)
    const inside = x >= 0 && x <= w && y >= 0 && y <= h
    return [Math.round(x * 10) / 10, Math.round(y * 10) / 10, j.on && inside ? 1 : 0]
  })
  return { points }
}

export function poseStrength(): number | null {
  return poseActive() ? num(input('pose_strength')) : null
}

export function poseStyle(): string | null {
  return poseActive() ? select('pose_style').value || null : null
}

/** Called when the family changes: the block only exists for Anima. */
export function showPose(): void {
  const anima = select('family').value === 'anima'
  $('pose-block').hidden = !anima
  $('pose-editor').hidden = !input('pose_on').checked
  if (anima) draw()
}

export function initPose(options: Options): void {
  const cfg = options.pose
  if (!cfg) {
    // An API without pose support: keep the block out of the form entirely.
    $('pose-block').remove()
    return
  }
  select('pose_style').replaceChildren(...cfg.styles.map((s) =>
    el('option', { value: s, selected: s === cfg.style_default }, s)))
  input('pose_strength').value = String(cfg.strength_default)
  select('pose_preset').replaceChildren(...Object.keys(PRESETS).map((p) => el('option', { value: p }, p)))
  select('pose_preset').addEventListener('change', () => load(select('pose_preset').value))
  $<HTMLButtonElement>('pose-reset').addEventListener('click', () => load(select('pose_preset').value))
  input('pose_on').addEventListener('change', showPose)
  // The canvas follows the scene's aspect.
  for (const id of ['width', 'height']) input(id).addEventListener('input', () => draw())
  bindCanvas()
  load('standing')
  showPose()
}
