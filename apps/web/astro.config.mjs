// @ts-check
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'astro/config'
import node from '@astrojs/node'

const repoRootUrl = new URL('../../', import.meta.url)
const repoRoot = fileURLToPath(repoRootUrl)

/**
 * Seed `process.env` from the repository-root `.env`.
 *
 * `envDir` below already points Astro at that file, but Vite only exposes it
 * through `import.meta.env`, which is resolved at build time. Everything in
 * this app reads configuration at request time from `process.env` instead,
 * because the CLI embeds the built server in its own process and sets the
 * variables there — a secret baked into the bundle would be the wrong value
 * and would also ship inside the artifact. This loop is what gives
 * `astro dev` and `astro preview` the same values without a second `.env`.
 * Anything already exported wins, so the CLI stays authoritative.
 */
function seedProcessEnv() {
  let raw = ''
  try {
    raw = readFileSync(new URL('.env', repoRootUrl), 'utf8')
  } catch {
    return // no .env on this machine: the launcher is expected to export them
  }
  for (const line of raw.split(/\r?\n/)) {
    if (line.trimStart().startsWith('#')) continue
    const match = /^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$/.exec(line)
    if (match === null) continue
    const [, key, rest] = match
    if (key === undefined || rest === undefined) continue
    let value = rest.trim()
    const quoted = value.length > 1 && (value.startsWith('"') || value.startsWith("'"))
    if (quoted && value.endsWith(value.slice(0, 1))) value = value.slice(1, -1)
    if (process.env[key] === undefined) process.env[key] = value
  }
}

seedProcessEnv()

export default defineConfig({
  output: 'server',
  // Single source of configuration: the same .env scripts/config.py reads.
  // Astro has no top-level `envDir`; it is a Vite option, and this is where
  // Astro exposes it. Pointing it at the repository root is what keeps a
  // second .env from ever appearing under apps/web.
  vite: { envDir: repoRoot },
  // Standalone so the adapter serves dist/client itself; the CLI imports
  // dist/server/entry.mjs and calls startServer() rather than re-implementing
  // static file handling. See docs/monorepo-contract.md.
  adapter: node({ mode: 'standalone' }),
  server: {
    host: process.env.WEB_HOST ?? '127.0.0.1',
    port: Number(process.env.WEB_PORT ?? 4321),
  },
})
