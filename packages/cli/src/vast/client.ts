import { CliError, EXIT } from '../lib/errors.ts'

export const CONSOLE_BASE = process.env['VAST_URL'] || 'https://console.vast.ai'

/**
 * The autoscaler is a separate service from the console API. Endpoint logs,
 * live workers and rolling updates live there, and only there.
 */
export function autoscalerBase(consoleBase: string): string {
  return consoleBase === 'https://console.vast.ai' ? 'https://run.vast.ai' : consoleBase
}

const RETRYABLE = new Set([429, 502, 503, 504])

export interface VastErrorBody {
  success?: boolean
  error?: string
  msg?: string
  [k: string]: unknown
}

/**
 * An API failure that carries Vast's own words.
 *
 * Vast answers a bad request with {"success": false, "error": "bad_request",
 * "msg": "<what is actually wrong>"} and HTTP 200 or 400 depending on the
 * route. Reporting only the status code throws away the one field that says
 * what to fix, so the message is rebuilt from the body whenever there is one.
 */
export class VastApiError extends CliError {
  readonly status: number
  readonly body: unknown
  readonly url: string

  constructor(status: number, url: string, body: unknown) {
    super(VastApiError.describe(status, url, body), { code: EXIT.UPSTREAM, details: body })
    this.name = 'VastApiError'
    this.status = status
    this.body = body
    this.url = url
  }

  private static describe(status: number, url: string, body: unknown): string {
    const path = safePath(url)
    if (body && typeof body === 'object') {
      const b = body as VastErrorBody
      const parts = [b.error, b.msg].filter((x): x is string => typeof x === 'string' && x.length > 0)
      if (parts.length) return `Vast API ${status} on ${path}: ${parts.join(': ')}`
      const asText = JSON.stringify(body)
      if (asText && asText !== '{}') return `Vast API ${status} on ${path}: ${truncate(asText, 400)}`
    }
    if (typeof body === 'string' && body.trim()) {
      return `Vast API ${status} on ${path}: ${truncate(body.trim(), 400)}`
    }
    return `Vast API ${status} on ${path}`
  }
}

function safePath(url: string): string {
  try {
    return new URL(url).pathname
  } catch {
    return url
  }
}

function truncate(s: string, n: number): string {
  return s.length <= n ? s : s.slice(0, n) + '…'
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE'
  body?: unknown
  query?: Record<string, string | number | boolean | object>
  timeoutMs?: number
  /** Absolute URL, used for the autoscaler service. */
  absolute?: string
  retries?: number
}

export class VastClient {
  readonly base: string
  private readonly apiKey: string
  private readonly defaultTimeout: number

  constructor(apiKey: string, opts: { base?: string; timeoutMs?: number } = {}) {
    if (!apiKey) {
      throw new CliError('No Vast API key.', {
        code: EXIT.CONFIG,
        hint: 'Set VAST_API_KEY in .env or run: vastai set api-key <KEY>',
      })
    }
    this.apiKey = apiKey
    this.base = opts.base ?? CONSOLE_BASE
    this.defaultTimeout = opts.timeoutMs ?? 120_000
  }

  get autoscaler(): string {
    return autoscalerBase(this.base)
  }

  private url(path: string, query?: RequestOptions['query'], absolute?: string): string {
    let full: string
    if (absolute) {
      full = absolute
    } else {
      const sub = /^\/api\/v\d+\//.test(path) ? path : '/api/v0' + path
      full = this.base + sub
    }
    if (query && Object.keys(query).length) {
      const qs = new URLSearchParams()
      for (const [k, v] of Object.entries(query)) {
        qs.set(k, typeof v === 'object' ? JSON.stringify(v) : String(v))
      }
      full += (full.includes('?') ? '&' : '?') + qs.toString()
    }
    return full
  }

  async request<T = unknown>(path: string, opts: RequestOptions = {}): Promise<T> {
    const url = this.url(path, opts.query, opts.absolute)
    const method = opts.method ?? 'GET'
    const retries = opts.retries ?? 3
    const timeoutMs = opts.timeoutMs ?? this.defaultTimeout

    let lastError: unknown = null
    for (let attempt = 0; attempt <= retries; attempt++) {
      const ac = new AbortController()
      const timer = setTimeout(() => ac.abort(), timeoutMs)
      try {
        const headers: Record<string, string> = {
          Authorization: `Bearer ${this.apiKey}`,
          Accept: 'application/json',
          'User-Agent': 'comfy-vast-cli/0.1.0',
        }
        const init: RequestInit = { method, headers, signal: ac.signal, redirect: 'follow' }
        if (opts.body !== undefined) {
          headers['Content-Type'] = 'application/json'
          init.body = JSON.stringify(opts.body)
        }

        const res = await fetch(url, init)
        const text = await res.text()
        const parsed = parseMaybeJson(text)

        if (!res.ok) {
          if (RETRYABLE.has(res.status) && attempt < retries) {
            await sleep(backoff(attempt, res.headers.get('retry-after')))
            continue
          }
          throw new VastApiError(res.status, url, parsed ?? text)
        }

        // Several console routes answer 200 with success: false. That is a
        // failure regardless of what the status line says.
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
          const b = parsed as VastErrorBody
          if (b.success === false) throw new VastApiError(res.status, url, b)
        }
        return (parsed ?? text) as T
      } catch (e) {
        if (e instanceof VastApiError) throw e
        lastError = e
        const transient = e instanceof Error && (e.name === 'AbortError' || e.name === 'TypeError')
        if (transient && attempt < retries) {
          await sleep(backoff(attempt, null))
          continue
        }
        break
      } finally {
        clearTimeout(timer)
      }
    }

    const reason = lastError instanceof Error ? lastError.message : String(lastError)
    throw new CliError(`Could not reach the Vast API (${safePath(url)}): ${reason}`, {
      code: EXIT.UPSTREAM,
      hint: 'Check network connectivity, then retry.',
    })
  }

  get<T>(path: string, query?: RequestOptions['query']): Promise<T> {
    return this.request<T>(path, query ? { query } : {})
  }

  post<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>(path, body === undefined ? { method: 'POST' } : { method: 'POST', body })
  }

  put<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>(path, body === undefined ? { method: 'PUT' } : { method: 'PUT', body })
  }

  delete<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>(path, body === undefined ? { method: 'DELETE' } : { method: 'DELETE', body })
  }

  /** POST to the autoscaler service, which wants the api key in the body. */
  autoscalerPost<T>(path: string, body: Record<string, unknown>): Promise<T> {
    return this.request<T>(path, {
      method: 'POST',
      absolute: this.autoscaler + path,
      body: { ...body, api_key: this.apiKey },
    })
  }
}

function parseMaybeJson(text: string): unknown {
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    return null
  }
}

function backoff(attempt: number, retryAfter: string | null): number {
  if (retryAfter) {
    const secs = Number(retryAfter)
    if (Number.isFinite(secs) && secs >= 0) return Math.min(secs * 1000, 30_000)
  }
  return Math.min(500 * 2 ** attempt, 8_000)
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms))
}
