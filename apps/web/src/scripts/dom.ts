// Small shared helpers. Still no framework: these are plain browser modules,
// bundled by Astro instead of being served raw, which is the only thing the
// migration changed about them.

/**
 * Elements the markup is required to contain. Throwing beats a null check at
 * every call site: if one of these is missing the page is broken anyway, and a
 * named error says which id went away.
 */
export function $<T extends HTMLElement = HTMLElement>(id: string): T {
  const node = document.getElementById(id)
  if (node === null) throw new Error(`autoscaler-vast console: missing element #${id}`)
  return node as T
}

export function $$<T extends Element = Element>(
  selector: string,
  root: ParentNode = document,
): T[] {
  return [...root.querySelectorAll<T>(selector)]
}

export const input = (id: string): HTMLInputElement => $<HTMLInputElement>(id)
export const area = (id: string): HTMLTextAreaElement => $<HTMLTextAreaElement>(id)
export const select = (id: string): HTMLSelectElement => $<HTMLSelectElement>(id)

/** mm:ss from seconds. */
export function fmt(seconds: number | null | undefined): string {
  const s = Math.max(0, Math.round(seconds ?? 0))
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}

/** Escape text going into innerHTML. Prompts contain quotes and angle brackets. */
export function esc(value: unknown): string {
  const table: Record<string, string> = {
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }
  return String(value ?? '').replace(/[&<>"']/g, (c) => table[c] ?? c)
}

/** Value of a numeric input, or null when left blank (= "use the default"). */
export function num(el: HTMLInputElement | null): number | null {
  return el === null || el.value === '' ? null : Number(el.value)
}

export function when(ts: number | undefined): string {
  return new Date((ts ?? 0) * 1000).toLocaleString()
}

type Attr = string | number | boolean | null | undefined | EventListener
type Child = Node | string | number | false | null | undefined

/** Build an element tree without a template engine. */
export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Record<string, Attr> = {},
  ...children: (Child | Child[])[]
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag)
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue
    if (key === 'class') node.className = String(value)
    else if (key === 'html') node.innerHTML = String(value)
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value as EventListener)
    else node.setAttribute(key, value === true ? '' : String(value))
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue
    node.append(child instanceof Node ? child : document.createTextNode(String(child)))
  }
  return node
}
