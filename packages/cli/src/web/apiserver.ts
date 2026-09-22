import { spawn, type ChildProcess } from 'node:child_process'

import { ApiClient } from '../lib/api.ts'
import type { Config } from '../lib/config.ts'
import { CliError, EXIT } from '../lib/errors.ts'
import { c, err } from '../lib/output.ts'
import { ROOT, SCRIPTS_DIR, WEBAPP_SERVER } from '../lib/paths.ts'

export interface ApiProcess {
  child: ChildProcess
  base: string
  stop(): Promise<void>
}

/**
 * Spawn `python webapp/server.py` and wait until it answers.
 *
 * The contract puts FastAPI on loopback and authentication in Astro, so this
 * never passes a public bind address; API_HOST/API_PORT are forwarded in the
 * environment and the readiness probe uses the same pair, which means a
 * mismatch surfaces as a timeout with the address printed rather than as a
 * silently unreachable API.
 */
export async function startApi(cfg: Config, opts: { quiet?: boolean; timeoutMs?: number } = {}): Promise<ApiProcess> {
  const api = new ApiClient(cfg)

  if (await api.healthy()) {
    if (!opts.quiet) err(c.dim(`api   already running on ${api.base}, reusing it`))
    return {
      child: null as unknown as ChildProcess,
      base: api.base,
      stop: async () => undefined,
    }
  }

  const child = spawn(cfg.pythonBin, [WEBAPP_SERVER], {
    cwd: ROOT,
    env: {
      ...process.env,
      PYTHONPATH: [SCRIPTS_DIR, process.env['PYTHONPATH']].filter(Boolean).join(process.platform === 'win32' ? ';' : ':'),
      PYTHONUNBUFFERED: '1',
      PYTHONIOENCODING: 'utf-8',
      API_HOST: cfg.apiHost,
      API_PORT: String(cfg.apiPort),
      ...(cfg.apiKey ? { VAST_API_KEY: cfg.apiKey } : {}),
    },
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  })

  let died: { code: number | null; signal: NodeJS.Signals | null } | null = null
  let tail = ''
  const collect = (chunk: string) => {
    tail = (tail + chunk).slice(-4000)
    if (!opts.quiet) process.stderr.write(c.dim(prefix(chunk, 'api   ')))
  }
  child.stdout?.setEncoding('utf8')
  child.stderr?.setEncoding('utf8')
  child.stdout?.on('data', collect)
  child.stderr?.on('data', collect)
  child.on('exit', (code, signal) => {
    died = { code, signal }
  })
  child.on('error', () => {
    died = { code: -1, signal: null }
  })

  const deadline = Date.now() + (opts.timeoutMs ?? 30_000)
  while (Date.now() < deadline) {
    // Read through a local: TypeScript narrows `died` to never here, because
    // every assignment to it happens inside a callback it cannot order.
    const exited = died as { code: number | null; signal: NodeJS.Signals | null } | null
    if (exited) {
      throw new CliError(`webapp/server.py exited before it was ready (code ${exited.code}).`, {
        code: EXIT.ERROR,
        hint: 'Run `python webapp/server.py` on its own to see the traceback.',
        details: tail,
      })
    }
    if (await api.healthy(1000)) {
      if (!opts.quiet) err(c.green(`api   up on ${api.base}`))
      return { child, base: api.base, stop: () => stopChild(child) }
    }
    await sleep(400)
  }

  await stopChild(child)
  throw new CliError(`webapp/server.py did not answer on ${api.base} within 30s.`, {
    code: EXIT.ERROR,
    hint:
      cfg.apiHost === '127.0.0.1' && cfg.apiPort === 8800
        ? 'Check the traceback above.'
        : `webapp/server.py may still be binding its own default 127.0.0.1:8800 rather than API_HOST/API_PORT (${api.base}).`,
    details: tail,
  })
}

export async function stopChild(child: ChildProcess | null): Promise<void> {
  if (!child || child.exitCode !== null || child.killed) return
  await new Promise<void>((resolve) => {
    const done = () => resolve()
    child.once('exit', done)
    // SIGTERM is not implemented on Windows; Node maps kill() to a hard stop
    // there, which is what taking the process down cleanly amounts to anyway.
    child.kill('SIGTERM')
    setTimeout(() => {
      if (child.exitCode === null && !child.killed) child.kill('SIGKILL')
      setTimeout(done, 500)
    }, 4000)
  })
}

function prefix(chunk: string, tag: string): string {
  return chunk
    .split(/\r?\n/)
    .filter((l) => l.length > 0)
    .map((l) => tag + l + '\n')
    .join('')
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms))
}
