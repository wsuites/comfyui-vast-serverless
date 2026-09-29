// The header strip: what is rented right now and what it has cost.

import { $, el, fmt } from './dom'
import type { LocalComfy, StatusResponse, Worker } from '../lib/types'
import { getStatus, rebootWorker, setBackend } from './api'

/**
 * GPU load, VRAM and temperature, when the host reports them.
 *
 * Deliberately not a progress bar. Vast refreshes this on its own schedule
 * (tens of seconds), so an 18 s render can pass between two samples without
 * ever showing 100 %. The title spells that out rather than letting a reading
 * of 0 % be mistaken for a stalled worker - the phase stepper is what says
 * whether a render is actually moving.
 */
function load(w: Worker): HTMLElement[] {
  const out: HTMLElement[] = []
  const hint = 'Reported by the host on Vast’s polling cadence, not live: '
    + 'a short render can finish between two samples.'
  if (w.gpu_util !== null && w.gpu_util !== undefined) {
    out.push(el('span', { title: hint },
      'gpu ', el('b', {}, `${w.gpu_util.toFixed(0)}%`),
      w.gpu_temp ? ` · ${w.gpu_temp.toFixed(0)}°C` : ''))
  }
  if (w.vram !== null && w.vram !== undefined) {
    out.push(el('span', { title: hint },
      'vram ', el('b', {}, `${w.vram.toFixed(1)}`),
      w.vram_total ? ` / ${w.vram_total.toFixed(0)} GB` : ' GB'))
  }
  // Not a warning about this render: it is a warning about the queue. The
  // endpoint holds the slot, so whatever you submit next waits behind it.
  if (w.reachable === false) {
    // Worse than wedged and not fixable from here: the machine is up but its
    // forwarded ports do not answer, so nothing we submit can ever arrive.
    out.push(el('b', { class: 'wedged', title:
      'The machine answers ping but its serving port is closed to this '
      + 'server. Requests cannot be delivered - the worker has to be '
      + 'replaced, not restarted.' }, `unreachable ${w.address ?? ''}`))
    return out
  }
  if (w.stalled) {
    out.push(el('span', { class: 'wedged', title:
      'The endpoint counts a request against this worker while its GPU is '
      + 'idle. Nothing is rendering and new requests queue until they time out.' },
      'slot wedged ', el('b', {}, fmt(w.stalled))))
  }
  return out
}

async function poll(): Promise<void> {
  const host = $('worker')
  let status: StatusResponse
  try {
    status = await getStatus()
  } catch {
    host.replaceChildren(el('span', { class: 'dot off' }), 'local server unreachable')
    return
  }
  const toggle = backendToggle(status)
  if (!status.backend || !status.local) {
    // An API from before the local backend existed: say so instead of throwing
    // on status.local and leaving the header frozen without the toggle.
    toggle.title = 'The API behind this page predates the local backend; redeploy it'
    for (const b of toggle.querySelectorAll('button')) b.disabled = true
  }
  if (status.backend === 'local') {
    host.replaceChildren(toggle, ...localStatus(status.local))
    return
  }
  const w = status.worker
  if (!w) {
    host.replaceChildren(
      toggle,
      el('span', {},
        el('span', { class: 'dot off' }),
        'no machine rented — nothing is being charged'),
    )
    return
  }
  host.replaceChildren(
    toggle,
    el('span', {},
      el('span', { class: 'dot on' }),
      el('b', {}, w.gpu ?? 'worker'),
      w.status ? ` · ${w.status}` : ''),
    ...load(w),
    el('span', {}, `$${Number(w.dph ?? 0).toFixed(3)}/h`),
    el('span', {}, 'up ', el('b', {}, fmt((w.hours ?? 0) * 3600))),
    el('span', {}, 'spent ', el('b', {}, `$${(w.spent ?? 0).toFixed(2)}`)),
    el('span', {}, `machine ${w.machine ?? '?'}`),
    rebootButton(w),
  )
}

/**
 * Vast / Local switch. It sets where *new* jobs render: the choice lives on
 * the API, so every open tab and ``cv job submit`` follow it, and a job that
 * is already running finishes where it started.
 */
function backendToggle(status: StatusResponse): HTMLElement {
  const wrap = el('span', { class: 'backend-toggle', title: 'Where new renders run' })
  for (const b of ['vast', 'local'] as const) {
    const btn = el('button', {
      class: b === status.backend ? 'active' : '',
      title: b === 'local'
        ? `ComfyUI container at ${status.local?.url ?? '(unknown)'}`
        : 'Rent a Vast worker',
    }, b === 'vast' ? 'Vast' : 'Local')
    btn.onclick = async () => {
      if (b === status.backend) return
      if (b === 'local' && !status.local?.ready
          && !window.confirm('The local ComfyUI is not answering right now. '
            + 'Switch anyway? Jobs will fail until it is up.')) return
      try {
        await setBackend(b)
      } catch (e) {
        window.alert(String(e instanceof Error ? e.message : e))
      }
      void poll()
    }
    wrap.append(btn)
  }
  return wrap
}

/** The local card: no rent, no cost, just whether ComfyUI answers. */
function localStatus(l: LocalComfy): HTMLElement[] {
  if (!l.ready) {
    return [el('span', { title: l.detail ?? '' },
      el('span', { class: 'dot off' }),
      `local ComfyUI down at ${l.url} — start the comfy-local container`)]
  }
  const out: HTMLElement[] = [
    el('span', {}, el('span', { class: 'dot on' }), el('b', {}, l.gpu ?? 'local GPU'), ' · local'),
  ]
  if (l.vram != null && l.vram_total != null) {
    out.push(el('span', {}, 'vram ', el('b', {}, `${l.vram.toFixed(1)} / ${l.vram_total.toFixed(1)} GB`)))
  }
  out.push(el('span', {}, 'free'))
  return out
}

/**
 * Restart the worker container from the header. The cure for a wedged slot.
 *
 * Measured: the slot does not free the moment the worker comes back - the
 * request count survives the restart by some minutes while the autoscaler
 * reconciles. The wedged flag next to it is what says when it is really gone.
 */
function rebootButton(w: Worker): HTMLButtonElement {
  const b = el('button', {
    class: 'worker-reboot',
    title: 'Stop/start the worker container: clears a wedged slot. Models '
      + 'stay on disk, so it is back in 2-3 min, but the request count can '
      + 'take a few minutes more to drop.',
  }, 'reboot')
  b.onclick = async () => {
    const ok = window.confirm(`Restart worker ${w.id ?? ''}?

`
      + 'It comes back in 2-3 minutes with its models still on disk. '
      + 'Anything it is holding right now is discarded.')
    if (!ok) return
    b.textContent = 'rebooting'
    b.disabled = true
    try {
      await rebootWorker()
    } catch (e) {
      // 409 while a render is genuinely in flight: the server refuses rather
      // than throwing away GPU minutes the operator is paying for.
      window.alert(String(e instanceof Error ? e.message : e))
      b.textContent = 'reboot'
      b.disabled = false
    }
  }
  return b
}

export function initWorker(everyMs = 10000): void {
  void poll()
  setInterval(() => void poll(), everyMs)
}
