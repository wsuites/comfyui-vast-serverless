// Reverse proxy from Astro to the FastAPI process.
//
// FastAPI is API-only and bound to loopback; the browser only ever talks to
// this server, which means /api/** and /results/** have to arrive there
// untouched — including GET /api/jobs/:id/stream, which is Server-Sent Events
// and is useless if anything in the path buffers it.

/** Headers that describe a single hop and must not be relayed to the next one. */
const HOP_BY_HOP = new Set([
  'connection',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'te',
  'trailer',
  'transfer-encoding',
  'upgrade',
])

const DROPPED_FROM_REQUEST = new Set([
  ...HOP_BY_HOP,
  // Belongs to this hop's connection, not to the upstream one.
  'host',
  // The body is forwarded as a stream, so undici re-frames it; a stale length
  // here would either truncate the body or hang waiting for bytes.
  'content-length',
  // This is the Basic Auth credential for *this* server. FastAPI has no use
  // for it and it must not travel further than it has to.
  'authorization',
  // Asking upstream not to compress keeps SSE flowing byte for byte and avoids
  // relaying a Content-Encoding that undici has already decoded away.
  'accept-encoding',
])

const DROPPED_FROM_RESPONSE = new Set([
  ...HOP_BY_HOP,
  // Recomputed by the Node server for the stream we hand it.
  'content-length',
  // undici decodes the upstream body, so the original encoding no longer
  // describes what we are about to write.
  'content-encoding',
])

export function upstreamBase(): string {
  // An absolute origin wins over the loopback pair, and is the only way a page
  // running on one machine drives the API on another: the workstation serves
  // the UI, the VPS owns the fleet. It points at the *public* hostname, which
  // fronts that machine's own Astro, not its FastAPI — the API is loopback-only
  // there by contract and nothing about that changes here.
  const origin = (process.env.API_ORIGIN || '').trim()
  if (origin) return origin.replace(/\/+$/, '')
  const host = process.env.API_HOST || '127.0.0.1'
  const port = process.env.API_PORT || '8800'
  return `http://${host}:${port}`
}

/**
 * The credential for a remote upstream, or null for the loopback one.
 *
 * A remote origin answers 401 before it answers anything else, so the page
 * would prompt for a password it cannot forward: the browser is authenticated
 * against *this* server, and that header is dropped one hop earlier on purpose.
 * These are the far machine's credentials, read from `.env` here and never
 * shown to the browser.
 */
export function upstreamAuth(): string | null {
  const user = (process.env.API_ORIGIN_USER || '').trim()
  const pass = process.env.API_ORIGIN_PASS || ''
  if (!user || !pass) return null
  return `Basic ${Buffer.from(`${user}:${pass}`).toString('base64')}`
}

/** Prefixes owned by FastAPI. Everything else is an Astro route. */
export function isProxied(pathname: string): boolean {
  return pathname === '/api'
    || pathname.startsWith('/api/')
    || pathname === '/results'
    || pathname.startsWith('/results/')
}

function isEventStream(response: Response): boolean {
  return (response.headers.get('content-type') ?? '').includes('text/event-stream')
}

export async function proxy(request: Request, url: URL): Promise<Response> {
  const target = new URL(url.pathname + url.search, upstreamBase())

  const headers = new Headers()
  for (const [name, value] of request.headers) {
    if (!DROPPED_FROM_REQUEST.has(name.toLowerCase())) headers.append(name, value)
  }
  // Set after the copy, so it replaces this hop's credential rather than
  // arriving alongside it.
  const credential = upstreamAuth()
  if (credential) headers.set('authorization', credential)

  // `duplex: 'half'` is required by the fetch spec to send a streaming body and
  // is what keeps an upload from being buffered into memory first.
  const init: RequestInit & { duplex?: 'half' } = {
    method: request.method,
    headers,
    redirect: 'manual',
    // Aborts the upstream request when the browser goes away, which is the
    // normal way an SSE connection ends. Without it the FastAPI generator
    // would keep polling Vast for a reader that no longer exists.
    signal: request.signal,
  }
  if (request.method !== 'GET' && request.method !== 'HEAD' && request.body !== null) {
    init.body = request.body
    init.duplex = 'half'
  }

  let upstream: Response
  try {
    upstream = await fetch(target, init)
  } catch (error) {
    // The client hung up mid-flight: nothing left to answer.
    if (request.signal.aborted) return new Response(null, { status: 499 })
    const reason = error instanceof Error ? error.message : String(error)
    const hint = process.env.API_ORIGIN
      ? 'Check that host is up and that its tunnel is running; this page only '
        + 'displays what it serves.'
      : 'Start it with: python webapp/server.py'
    return new Response(
      JSON.stringify({
        detail: `Cannot reach the API at ${upstreamBase()}: ${reason}. ${hint}`,
      }),
      { status: 502, headers: { 'content-type': 'application/json' } },
    )
  }

  const out = new Headers()
  for (const [name, value] of upstream.headers) {
    if (!DROPPED_FROM_RESPONSE.has(name.toLowerCase())) out.append(name, value)
  }
  if (isEventStream(upstream)) {
    out.set('cache-control', 'no-cache')
    out.set('connection', 'keep-alive')
    // Belt and braces for whatever reverse proxy or tunnel sits in front.
    out.set('x-accel-buffering', 'no')
  }

  // The body is passed through as a ReadableStream, never awaited into memory,
  // so phase updates reach the page as FastAPI emits them.
  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: out,
  })
}
