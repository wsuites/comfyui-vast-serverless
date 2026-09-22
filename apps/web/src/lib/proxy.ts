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
  const host = process.env.API_HOST || '127.0.0.1'
  const port = process.env.API_PORT || '8800'
  return `http://${host}:${port}`
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
    return new Response(
      JSON.stringify({
        detail: `Cannot reach the API at ${upstreamBase()}: ${reason}. `
          + 'Start it with: python webapp/server.py',
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
