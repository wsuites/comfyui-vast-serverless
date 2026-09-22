// Every request enters here: authenticate first, then either proxy it to
// FastAPI or let Astro render it.

import type { MiddlewareHandler } from 'astro'
import { checkAuth } from './lib/auth'
import { isProxied, proxy } from './lib/proxy'

/** Readiness probe for `cv web up`, which polls it before opening a tunnel. */
const PUBLIC_PATHS = new Set(['/healthz'])

export const onRequest: MiddlewareHandler = async (context, next) => {
  const url = new URL(context.request.url)

  if (!PUBLIC_PATHS.has(url.pathname)) {
    const rejection = checkAuth(context.request)
    if (rejection !== null) return rejection
  }

  if (isProxied(url.pathname)) return proxy(context.request, url)

  return next()
}
