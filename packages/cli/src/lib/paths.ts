import { existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * Locate the repository root.
 *
 * The CLI is always executed from inside the workspace (`packages/cli/dist`),
 * so walking up until `scripts/config.py` appears is exact and does not depend
 * on the current working directory. Falls back to cwd for odd installs.
 */
function findRoot(): string {
  let dir = dirname(fileURLToPath(import.meta.url))
  for (let i = 0; i < 12; i++) {
    if (existsSync(join(dir, 'scripts', 'config.py')) && existsSync(join(dir, 'pnpm-workspace.yaml'))) {
      return dir
    }
    const up = dirname(dir)
    if (up === dir) break
    dir = up
  }
  return process.cwd()
}

export const ROOT = findRoot()
export const ENV_FILE = join(ROOT, '.env')
export const ENV_EXAMPLE = join(ROOT, '.env.example')
export const SCRIPTS_DIR = join(ROOT, 'scripts')
export const WEBAPP_SERVER = join(ROOT, 'webapp', 'server.py')
export const ASTRO_ENTRY = join(ROOT, 'apps', 'web', 'dist', 'server', 'entry.mjs')

/** Expand a leading `~` the way the shell and `scripts/config.py` consumers do. */
export function expandHome(p: string): string {
  if (p === '~') return home()
  if (p.startsWith('~/') || p.startsWith('~\\')) return join(home(), p.slice(2))
  return p
}

export function home(): string {
  return process.env.USERPROFILE || process.env.HOME || ''
}

/** Resolve a possibly relative path against the repository root. */
export function fromRoot(p: string): string {
  return resolve(ROOT, expandHome(p))
}
