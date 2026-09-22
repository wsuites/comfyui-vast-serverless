// HTTP Basic Auth for the whole site.
//
// FastAPI binds loopback only and has no authentication of its own, so this is
// the only thing standing between a Cloudflare tunnel and the Vast API key the
// Python process holds. See docs/monorepo-contract.md, "Process topology".

import { Buffer } from 'node:buffer'
import { createHash, timingSafeEqual } from 'node:crypto'

export const REALM = 'comfy-vast'

interface AuthConfig {
  enabled: boolean
  user: string
  pass: string
}

/**
 * Read at request time, never at module scope: the CLI imports the built
 * entry.mjs and sets these variables around it, so a value captured while the
 * module was first evaluated could be the stale one.
 */
function authConfig(): AuthConfig {
  return {
    enabled: (process.env.WEBAPP_AUTH ?? 'on').trim().toLowerCase() !== 'off',
    user: process.env.WEBAPP_USER ?? 'admin',
    pass: process.env.WEBAPP_PASS ?? '',
  }
}

/**
 * Compare two secrets without leaking their contents or their length.
 *
 * `timingSafeEqual` throws when the buffers differ in size, and bailing out on
 * that check would turn the password length into an oracle. Hashing first
 * makes both sides a fixed 32 bytes, so the same work happens for every input.
 */
function constantTimeEquals(a: string, b: string): boolean {
  const left = createHash('sha256').update(a, 'utf8').digest()
  const right = createHash('sha256').update(b, 'utf8').digest()
  return timingSafeEqual(left, right)
}

interface Credentials {
  user: string
  pass: string
}

function parseBasic(header: string | null): Credentials {
  const empty: Credentials = { user: '', pass: '' }
  if (header === null) return empty
  const space = header.indexOf(' ')
  if (space < 0) return empty
  if (header.slice(0, space).toLowerCase() !== 'basic') return empty
  let decoded = ''
  try {
    decoded = Buffer.from(header.slice(space + 1).trim(), 'base64').toString('utf8')
  } catch {
    return empty
  }
  const colon = decoded.indexOf(':')
  if (colon < 0) return empty
  return { user: decoded.slice(0, colon), pass: decoded.slice(colon + 1) }
}

function unauthorized(): Response {
  return new Response('Authentication required.\n', {
    status: 401,
    headers: {
      'www-authenticate': `Basic realm="${REALM}"`,
      'content-type': 'text/plain; charset=utf-8',
      'cache-control': 'no-store',
    },
  })
}

function misconfigured(): Response {
  return new Response(
    'WEBAPP_AUTH is on but WEBAPP_PASS is empty.\n\n'
      + 'Refusing to serve: this process fronts a FastAPI server that holds the\n'
      + 'Vast API key, so running it unauthenticated would publish that key to\n'
      + 'anyone who can reach this port. Set WEBAPP_PASS in the repository-root\n'
      + '.env, or set WEBAPP_AUTH=off if this port is genuinely private.\n',
    {
      status: 503,
      headers: { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' },
    },
  )
}

/** `null` when the request may proceed, otherwise the response to send back. */
export function checkAuth(request: Request): Response | null {
  const config = authConfig()
  if (!config.enabled) return null
  if (config.pass === '') return misconfigured()

  const sent = parseBasic(request.headers.get('authorization'))
  // Both comparisons run before either result is inspected, so a wrong
  // username costs exactly as much as a wrong password.
  const userOk = constantTimeEquals(sent.user, config.user)
  const passOk = constantTimeEquals(sent.pass, config.pass)
  return userOk && passOk ? null : unauthorized()
}
