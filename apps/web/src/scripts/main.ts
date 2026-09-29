// Wiring: tabs, one-time catalogue fetch, and the three views.

import type { Options } from '../lib/types'
import { $, $$ } from './dom'
import { getOptions } from './api'
import { attachScene, initScene } from './scene'
import { initCompare } from './compare'
import { initGallery, refreshGallery } from './gallery'
import { initJobs, refreshJobs } from './jobs'
import { initWorker } from './worker'
import { initRetouch, openRetouch } from './retouch'
import { setRetouch } from './zoom'

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

/**
 * Banner when the page and the API were not deployed from the same commit.
 * A server too old to report a version counts as different: that is the
 * case this exists for (a `git pull` that never got its restart). Unknown on
 * the web side (built outside git) stays silent rather than crying wolf.
 */
function warnOnVersionDrift(api: string | null | undefined): void {
  const web = __WEB_VERSION__
  if (web === null || api === web) return
  const apiText = api ?? 'an older build that does not report one'
  document.body.prepend(Object.assign(document.createElement('div'), {
    className: 'err version-drift',
    style: 'padding:10px 20px; border-bottom:1px solid var(--bad)',
    textContent: `Version mismatch: this page is ${web}, the autoscaler API is ${apiText}. `
      + 'Features may fail until both are deployed from the same commit '
      + '(git pull, pnpm web:build, restart cv-api and cv-web).',
  }))
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

  warnOnVersionDrift(options.version)

  // A finished job is a new history entry; refresh so the tab is never stale.
  initScene(options, () => void refreshGallery())
  initCompare(options, () => void refreshGallery())

  // Picking a job off the strip re-attaches the scene view to it, whichever
  // tab and whichever session started it. Arms render there too: the view
  // paints any snapshot it is handed.
  // Retouch opens from the zoom modal on any tab; the job it queues is
  // followed in the scene view, like any other render.
  initRetouch(options, (jobId) => {
    show('scene')
    attachScene(jobId)
    refreshJobs()
  })
  setRetouch(openRetouch)

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
