// Wiring: tabs, one-time catalogue fetch, and the three views.

import type { Options } from '../lib/types'
import { $, $$ } from './dom'
import { getOptions } from './api'
import { attachScene, initScene } from './scene'
import { initCompare } from './compare'
import { initGallery, refreshGallery } from './gallery'
import { initJobs } from './jobs'
import { initWorker } from './worker'

const VIEWS = ['scene', 'compare', 'gallery'] as const
type View = (typeof VIEWS)[number]

function isView(name: string | undefined): name is View {
  return name !== undefined && (VIEWS as readonly string[]).includes(name)
}

function show(name: View): void {
  for (const v of VIEWS) $(`view-${v}`).hidden = v !== name
  for (const b of $$<HTMLButtonElement>('#tabs button')) {
    b.setAttribute('aria-selected', String(b.dataset['tab'] === name))
  }
  location.hash = name
  if (name === 'gallery') void refreshGallery()
}

async function boot(): Promise<void> {
  initWorker()
  initGallery()

  let options: Options
  try {
    options = await getOptions()
  } catch (e) {
    const reason = e instanceof Error ? e.message : String(e)
    document.body.prepend(Object.assign(document.createElement('div'), {
      className: 'err',
      style: 'padding:14px 20px',
      textContent: `The local server is not answering (${reason}). `
        + 'Start it with: python webapp/server.py',
    }))
    return
  }

  // A finished job is a new history entry; refresh so the tab is never stale.
  initScene(options, () => void refreshGallery())
  initCompare(options, () => void refreshGallery())

  // Picking a job off the strip re-attaches the scene view to it, whichever
  // tab and whichever session started it. Arms render there too: the view
  // paints any snapshot it is handed.
  initJobs((jobId) => {
    show('scene')
    attachScene(jobId)
  })

  $('tabs').addEventListener('click', (ev) => {
    const target = ev.target
    const tab = target instanceof Element ? target.closest('button')?.dataset['tab'] : undefined
    if (isView(tab)) show(tab)
  })
  const fromHash = location.hash.slice(1)
  show(isView(fromHash) ? fromHash : 'scene')
}

void boot()
