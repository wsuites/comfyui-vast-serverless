import type { APIRoute } from 'astro'

// Deliberately unauthenticated (see PUBLIC_PATHS in src/middleware.ts): the CLI
// polls this to know the server is up, before it has any reason to hold
// credentials. It reports nothing about the machine.
export const prerender = false

export const GET: APIRoute = () =>
  new Response('ok\n', {
    headers: {
      'content-type': 'text/plain; charset=utf-8',
      'cache-control': 'no-store',
    },
  })
