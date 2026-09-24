import type { Config } from './config.ts'
import { CliError, EXIT } from './errors.ts'

/**
 * Client for the FastAPI process in `webapp/server.py`.
 *
 * Per the monorepo contract that server binds loopback only, so on the machine
 * that runs it this talks to API_HOST:API_PORT directly rather than through
 * Astro. When `API_ORIGIN` is set the server is on another machine and its
 * loopback is not ours: the address is then the public one, which fronts *that*
 * machine's Astro, and the request carries that deployment's Basic credential.
 */
export interface JobSnapshot {
  id: string
  state: 'running' | 'done' | 'error' | string
  phase: string
  detail: string
  worker: Record<string, unknown> | null
  images: string[]
  error: string | null
  latency: number | null
  elapsed: number
  log: { seq: number; t: number; phase: string; detail: string }[]
  params: Record<string, unknown>
  kind: string
  label: string | null
  pair: string | null
  side: string | null
  created: number
}

/** The address this CLI reaches the API on: remote origin first, else loopback. */
export function apiBase(cfg: Config): string {
  if (cfg.apiOrigin) return cfg.apiOrigin
  const host = cfg.apiHost.includes(':') ? `[${cfg.apiHost}]` : cfg.apiHost
  return `http://${host}:${cfg.apiPort}`
}

/** The Basic header for a remote origin, or null when talking to loopback. */
export function apiAuthHeader(cfg: Config): string | null {
  if (!cfg.apiOrigin || !cfg.apiOriginUser || !cfg.apiOriginPass) return null
  const raw = `${cfg.apiOriginUser}:${cfg.apiOriginPass}`
  return `Basic ${Buffer.from(raw).toString('base64')}`
}

export class ApiClient {
  readonly base: string
  private readonly remote: boolean
  private readonly auth: string | null

  constructor(cfg: Config) {
    this.base = apiBase(cfg)
    this.remote = Boolean(cfg.apiOrigin)
    this.auth = apiAuthHeader(cfg)
  }

  /** `init.headers` with the upstream credential added, when there is one. */
  private headers(init?: RequestInit): Headers {
    const h = new Headers(init?.headers)
    if (this.auth) h.set('authorization', this.auth)
    return h
  }

  private async call<T>(path: string, init?: RequestInit): Promise<T> {
    let res: Response
    try {
      res = await fetch(this.base + path, { ...init, headers: this.headers(init) })
    } catch (e) {
      throw new CliError(`The API at ${this.base} is not answering.`, {
        code: EXIT.UPSTREAM,
        hint: this.remote
          ? 'That host serves it; check its cv-api and cv-tunnel units, not this machine.'
          : 'Start it with `cv web up`, or `python webapp/server.py` on its own.',
        details: e instanceof Error ? e.message : e,
      })
    }
    const text = await res.text()
    if (!res.ok) {
      let detail = text
      try {
        const body = JSON.parse(text) as { detail?: unknown }
        if (body.detail !== undefined) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
      } catch {
        // keep the raw text
      }
      throw new CliError(`API ${res.status} on ${path}: ${detail}`, {
        code: res.status === 404 ? EXIT.NOT_FOUND : EXIT.UPSTREAM,
      })
    }
    return (text ? JSON.parse(text) : null) as T
  }

  /** True when the server answers at all. Used for health gating. */
  async healthy(timeoutMs = 1500): Promise<boolean> {
    const ac = new AbortController()
    const timer = setTimeout(() => ac.abort(), timeoutMs)
    try {
      const res = await fetch(this.base + '/api/jobs', {
        signal: ac.signal,
        headers: this.headers(),
      })
      return res.ok
    } catch {
      return false
    } finally {
      clearTimeout(timer)
    }
  }

  submit(params: Record<string, unknown>): Promise<{ job_id: string }> {
    return this.call('/api/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
    })
  }

  list(): Promise<{ jobs: JobSnapshot[] }> {
    return this.call('/api/jobs')
  }

  get(id: string): Promise<JobSnapshot> {
    return this.call(`/api/jobs/${encodeURIComponent(id)}`)
  }

  status(): Promise<Record<string, unknown>> {
    return this.call('/api/status')
  }

  options(): Promise<Record<string, unknown>> {
    return this.call('/api/options')
  }

  history(limit = 200): Promise<{ items: Record<string, unknown>[] }> {
    return this.call(`/api/history?limit=${limit}`)
  }

  /**
   * Consume `GET /api/jobs/:id/stream`.
   *
   * The server emits one `data:` frame per second unconditionally, so the
   * parser only has to handle that one field; anything else is ignored rather
   * than treated as an error.
   */
  async *stream(id: string, signal?: AbortSignal): AsyncGenerator<JobSnapshot> {
    const init: RequestInit = { headers: { Accept: 'text/event-stream' } }
    if (signal) init.signal = signal

    let res: Response
    try {
      res = await fetch(`${this.base}/api/jobs/${encodeURIComponent(id)}/stream`, init)
    } catch (e) {
      throw new CliError(`The API at ${this.base} is not answering.`, {
        code: EXIT.UPSTREAM,
        hint: 'Start it with `cv web up`.',
        details: e instanceof Error ? e.message : e,
      })
    }
    if (!res.ok) {
      throw new CliError(`API ${res.status} while opening the stream for job ${id}.`, {
        code: res.status === 404 ? EXIT.NOT_FOUND : EXIT.UPSTREAM,
      })
    }
    if (!res.body) throw new CliError('The stream carried no body.', { code: EXIT.UPSTREAM })

    const decoder = new TextDecoder()
    let buffer = ''
    for await (const chunk of res.body as unknown as AsyncIterable<Uint8Array>) {
      buffer += decoder.decode(chunk, { stream: true })
      let sep: number
      while ((sep = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, sep)
        buffer = buffer.slice(sep + 2)
        for (const line of frame.split('\n')) {
          if (!line.startsWith('data:')) continue
          const payload = line.slice(5).trim()
          if (!payload) continue
          try {
            yield JSON.parse(payload) as JobSnapshot
          } catch {
            // a truncated frame is not fatal; the next one carries full state
          }
        }
      }
    }
  }
}

/** Phase order, mirrored from `scripts/vast_state.py:PHASES`. */
export const PHASES = ['submitting', 'renting', 'booting', 'provisioning', 'generating', 'saving', 'done'] as const

/** Labels, mirrored from `scripts/vast_state.py:LABELS`. */
export const PHASE_LABELS: Record<string, string> = {
  submitting: 'queued',
  renting: 'renting GPU',
  booting: 'booting image',
  provisioning: 'loading models',
  generating: 'rendering',
  saving: 'fetching image',
  done: 'done',
  error: 'failed',
}
