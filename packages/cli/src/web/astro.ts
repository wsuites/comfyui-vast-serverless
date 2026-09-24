import { existsSync } from 'node:fs'
import { pathToFileURL } from 'node:url'

import type { Config } from '../lib/config.ts'
import { CliError, EXIT } from '../lib/errors.ts'
import { ASTRO_ENTRY } from '../lib/paths.ts'

/**
 * The shape `@astrojs/node` v11 standalone returns from `startServer()`.
 * Fixed by the adapter and restated in docs/monorepo-contract.md.
 */
interface AstroServer {
  server: {
    host: string
    port: number
    stop(): Promise<void>
    closed(): Promise<void>
    server: unknown
  }
  done: Promise<void>
}

interface AstroEntry {
  startServer?: () => AstroServer
  handler?: unknown
  options?: unknown
}

export interface EmbeddedSite {
  host: string
  port: number
  url: string
  stop(): Promise<void>
}

/**
 * The environment the Astro server reads at request time.
 *
 * The site never parses `.env` itself — the CLI is the only thing that resolves
 * configuration, so whatever it decided has to be handed over explicitly. This
 * covers both halves of the contract: the credentials the middleware checks,
 * and the FastAPI address it proxies to. Without this the site sees an empty
 * WEBAPP_PASS and correctly refuses to serve anything.
 */
export function serverEnv(cfg: Config): Record<string, string> {
  return {
    ASTRO_NODE_AUTOSTART: 'disabled',
    HOST: cfg.webHost,
    PORT: String(cfg.webPort),
    API_HOST: cfg.apiHost,
    API_PORT: String(cfg.apiPort),
    // Empty is the loopback case, and an empty string is how the proxy spells
    // "not set": these are always written, so a stale value from the parent
    // shell cannot outlive a `.env` that no longer has one.
    API_ORIGIN: cfg.apiOrigin,
    API_ORIGIN_USER: cfg.apiOriginUser,
    API_ORIGIN_PASS: cfg.apiOriginPass,
    WEBAPP_AUTH: cfg.webappAuth ? 'on' : 'off',
    WEBAPP_USER: cfg.webappUser,
    WEBAPP_PASS: cfg.webappPass,
  }
}

/**
 * Embed the built Astro site in this process.
 *
 * Standalone mode means the adapter also serves dist/client/**, so the CLI
 * never reimplements static file handling. Autostart is disabled *before* the
 * import because the module starts listening at import time otherwise, and
 * HOST/PORT are environment variables because that is how the adapter reads
 * them.
 */
export async function startAstro(cfg: Config): Promise<EmbeddedSite> {
  requireBuild()

  // Assigned before the import, and never after: the module begins listening
  // as it is evaluated, so a later write would race the first request.
  for (const [key, value] of Object.entries(serverEnv(cfg))) {
    process.env[key] = value
  }

  let mod: AstroEntry
  try {
    mod = (await import(pathToFileURL(ASTRO_ENTRY).href)) as AstroEntry
  } catch (e) {
    throw new CliError(`The Astro server build could not be loaded: ${e instanceof Error ? e.message : String(e)}`, {
      code: EXIT.ERROR,
      hint: 'Rebuild it with: pnpm web:build',
    })
  }

  if (typeof mod.startServer !== 'function') {
    throw new CliError(`${ASTRO_ENTRY} does not export startServer().`, {
      code: EXIT.ERROR,
      hint: 'apps/web must build with @astrojs/node in mode: "standalone". Rebuild with: pnpm web:build',
    })
  }

  let started: AstroServer
  try {
    started = mod.startServer()
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e)
    if (/EADDRINUSE/.test(msg)) {
      throw new CliError(`Port ${cfg.webPort} is already in use.`, {
        code: EXIT.ERROR,
        hint: 'Pass --port N, or change WEB_PORT in .env.',
      })
    }
    throw new CliError(`Astro refused to start: ${msg}`, { code: EXIT.ERROR })
  }

  const host = started.server.host || cfg.webHost
  const port = started.server.port || cfg.webPort
  const shown = host === '0.0.0.0' || host === '::' ? '127.0.0.1' : host

  return {
    host,
    port,
    url: `http://${shown}:${port}`,
    stop: async () => {
      try {
        await started.server.stop()
      } catch {
        // already down
      }
    },
  }
}

/** The friendly version of a module-not-found. */
export function requireBuild(): void {
  if (existsSync(ASTRO_ENTRY)) return
  throw new CliError('The web UI has not been built yet.', {
    code: EXIT.CONFIG,
    hint:
      `Expected ${ASTRO_ENTRY}\n` +
      '      Build it with:  pnpm web:build\n' +
      '      (apps/web builds with @astrojs/node in standalone mode; the CLI\n' +
      '       imports its dist/server/entry.mjs rather than spawning a second Node.)',
  })
}

export function astroBuildExists(): boolean {
  return existsSync(ASTRO_ENTRY)
}
