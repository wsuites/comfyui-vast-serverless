import Table from 'cli-table3'
import pc from 'picocolors'

/**
 * Colour is dropped when stdout is not a TTY, when NO_COLOR is set, or when
 * --json was asked for: machine-readable output must never carry escapes.
 */
let colourEnabled = process.stdout.isTTY === true && !process.env['NO_COLOR']

export function setColour(on: boolean): void {
  colourEnabled = on
}

export function isTTY(): boolean {
  return process.stdout.isTTY === true
}

type Paint = (s: string) => string
const paint = (fn: Paint): Paint => (s) => (colourEnabled ? fn(s) : s)

export const c = {
  bold: paint(pc.bold),
  dim: paint(pc.dim),
  red: paint(pc.red),
  green: paint(pc.green),
  yellow: paint(pc.yellow),
  blue: paint(pc.blue),
  cyan: paint(pc.cyan),
  magenta: paint(pc.magenta),
  gray: paint(pc.gray),
}

export function out(line = ''): void {
  process.stdout.write(line + '\n')
}

export function err(line = ''): void {
  process.stderr.write(line + '\n')
}

export function json(value: unknown): void {
  process.stdout.write(JSON.stringify(value, null, 2) + '\n')
}

export function table(head: string[], rows: (string | number)[][]): string {
  const t = new Table({
    head: head.map((h) => c.bold(h)),
    style: { head: [], border: [], compact: true },
    chars: {
      top: '', 'top-mid': '', 'top-left': '', 'top-right': '',
      bottom: '', 'bottom-mid': '', 'bottom-left': '', 'bottom-right': '',
      left: '', 'left-mid': '', mid: '', 'mid-mid': '',
      right: '', 'right-mid': '', middle: '  ',
    },
  })
  for (const r of rows) t.push(r.map((cell) => String(cell)))
  return t.toString()
}

export function printTable(head: string[], rows: (string | number)[][], empty = 'nothing to show'): void {
  if (rows.length === 0) {
    out(c.dim(empty))
    return
  }
  out(table(head, rows))
}

/** Right-pad/truncate for fixed-width fields inside a single line. */
export function fit(s: string, width: number): string {
  if (s.length <= width) return s.padEnd(width)
  return s.slice(0, Math.max(0, width - 1)) + '…'
}

export function fmtDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '-'
  const s = Math.floor(seconds % 60)
  const m = Math.floor((seconds / 60) % 60)
  const h = Math.floor(seconds / 3600)
  if (h > 0) return `${h}h${String(m).padStart(2, '0')}m`
  if (m > 0) return `${m}m${String(s).padStart(2, '0')}s`
  return `${s}s`
}

export function fmtBytes(n: number): string {
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v >= 10 || i === 0 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`
}

export function fmtMoney(n: number | null | undefined, digits = 4): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '-'
  return `$${n.toFixed(digits)}`
}

/** Mask a secret, keeping enough of the head to recognise which one it is. */
export function mask(value: string | undefined | null, keep = 4): string {
  if (!value) return ''
  if (value.length <= keep) return '*'.repeat(value.length)
  return value.slice(0, keep) + '*'.repeat(Math.min(12, value.length - keep))
}
