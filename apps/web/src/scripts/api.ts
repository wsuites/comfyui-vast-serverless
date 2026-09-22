// Everything that talks to webapp/server.py. The Vast API key never reaches the
// browser: the local server holds it. These paths are relative, so they land on
// the Astro origin and the middleware forwards them to FastAPI — the API port
// itself is never exposed.

import type {
  CompareRequest,
  CompareResponse,
  HistoryResponse,
  Job,
  JobCreated,
  JobsResponse,
  Options,
  SceneRequest,
  StatusResponse,
} from '../lib/types'

async function post<T>(path: string, body: unknown): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  } catch (e) {
    throw new Error(`Cannot reach the local server: ${String(e)}`)
  }
  if (!res.ok) throw new Error(`Rejected: ${await res.text()}`)
  return res.json() as Promise<T>
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) throw new Error(`${path}: ${res.status}`)
  return res.json() as Promise<T>
}

export const submitScene = (body: SceneRequest): Promise<JobCreated> => post('/api/jobs', body)
export const submitCompare = (body: CompareRequest): Promise<CompareResponse> =>
  post('/api/compare', body)
export const getOptions = (): Promise<Options> => get('/api/options')
/**
 * Every job the server currently knows about, running or not.
 *
 * The page keeps no state of its own, so this is the only way a render
 * survives a reload — or becomes visible at all when it was started from the
 * CLI rather than from this form.
 */
export const getJobs = (): Promise<JobsResponse> => get('/api/jobs')
export const getStatus = (): Promise<StatusResponse> => get('/api/status')

/**
 * Stop waiting for a running job. Returns its final snapshot.
 *
 * This releases the console, not the GPU: the request is already at the
 * endpoint and the api wrapper has no interrupt, so the worker finishes what
 * it started and the image is thrown away. Cancelling one arm of a compare
 * cancels the pair.
 */
export const cancelJob = (jobId: string): Promise<Job> =>
  post(`/api/jobs/${jobId}/cancel`, {})
/**
 * Restart the worker container. 409 while the card is genuinely rendering:
 * the server will not throw away a live render to clear a queue.
 */
export const rebootWorker = (): Promise<{ ok: boolean; instance?: string; detail?: string; hazard?: string }> =>
  post('/api/worker/reboot', {})

export const getHistory = (limit = 200): Promise<HistoryResponse> =>
  get(`/api/history?limit=${limit}`)

/**
 * Follow one job over SSE until it stops running.
 *
 * The stream sends a full snapshot once per second, so a dropped frame costs
 * nothing: the next one carries the whole state again.
 */
export function follow(
  jobId: string,
  onSnapshot: (job: Job) => void,
  onEnd?: (job: Job | null) => void,
): EventSource {
  const stream = new EventSource(`/api/jobs/${jobId}/stream`)
  let last: Job | null = null
  stream.onmessage = (m: MessageEvent<string>) => {
    last = JSON.parse(m.data) as Job
    onSnapshot(last)
    if (last.state !== 'running') {
      stream.close()
      onEnd?.(last)
    }
  }
  stream.onerror = () => {
    stream.close()
    onEnd?.(last)
  }
  return stream
}
