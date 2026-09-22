// The header strip: what is rented right now and what it has cost.

import { $, el, fmt } from './dom'
import type { Worker } from '../lib/types'
import { getStatus, rebootWorker } from './api'

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
  let w
  try {
    w = (await getStatus()).worker
  } catch {
    host.replaceChildren(el('span', { class: 'dot off' }), 'local server unreachable')
    return
  }
  if (!w) {
    host.replaceChildren(
      el('span', { class: 'dot off' }),
      'no machine rented — nothing is being charged',
    )
    return
  }
  host.replaceChildren(
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
