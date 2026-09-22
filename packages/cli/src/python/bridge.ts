import { spawn, type ChildProcess } from 'node:child_process'

import type { Config } from '../lib/config.ts'
import { CliError, EXIT } from '../lib/errors.ts'
import { ROOT, SCRIPTS_DIR } from '../lib/paths.ts'

export interface RunOptions {
  /** Script path relative to the repo root, e.g. "scripts/call_endpoint.py". */
  script: string
  args: string[]
  cfg: Config
  /** Mirror the child's stdout into the parent's stdout. */
  passthroughStdout?: boolean
  /** Called for each complete line the child writes, on either stream. */
  onLine?: (line: string, stream: 'stdout' | 'stderr') => void
  env?: Record<string, string>
}

export interface RunResult {
  code: number
  stdout: string
  stderr: string
}

/**
 * Run one of the Python scripts and stream it through.
 *
 * The workflow builder is 804 lines of node graph surgery that already works;
 * the CLI drives it rather than reimplementing it. Output is both forwarded
 * live (a cold start is minutes of silence otherwise) and captured, so a
 * command can still parse the result afterwards.
 *
 * `PYTHONPATH` gains `scripts/` because the scripts import `config` as a
 * top-level module, which only resolves when they are run from there.
 */
export function runPython(opts: RunOptions): Promise<RunResult> & { child: ChildProcess } {
  const { cfg } = opts
  const env: NodeJS.ProcessEnv = {
    ...process.env,
    PYTHONPATH: [SCRIPTS_DIR, process.env['PYTHONPATH']].filter(Boolean).join(delimiter()),
    PYTHONUNBUFFERED: '1',
    PYTHONIOENCODING: 'utf-8',
    ...opts.env,
  }
  if (cfg.apiKey) env['VAST_API_KEY'] = cfg.apiKey

  let child: ChildProcess
  try {
    child = spawn(cfg.pythonBin, [opts.script, ...opts.args], {
      cwd: ROOT,
      env,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    })
  } catch (e) {
    throw new CliError(`Could not start ${cfg.pythonBin}: ${e instanceof Error ? e.message : String(e)}`, {
      code: EXIT.CONFIG,
      hint: 'Set PYTHON_BIN in .env to the interpreter that has the vastai package.',
    })
  }

  const promise = new Promise<RunResult>((resolve, reject) => {
    let stdout = ''
    let stderr = ''

    const pump = (stream: 'stdout' | 'stderr') => {
      let buffer = ''
      const src = stream === 'stdout' ? child.stdout : child.stderr
      src?.setEncoding('utf8')
      src?.on('data', (chunk: string) => {
        if (stream === 'stdout') stdout += chunk
        else stderr += chunk

        if (stream === 'stderr') process.stderr.write(chunk)
        else if (opts.passthroughStdout) process.stdout.write(chunk)

        if (opts.onLine) {
          buffer += chunk
          let nl: number
          while ((nl = buffer.indexOf('\n')) !== -1) {
            const line = buffer.slice(0, nl).replace(/\r$/, '')
            buffer = buffer.slice(nl + 1)
            opts.onLine(line, stream)
          }
        }
      })
    }
    pump('stdout')
    pump('stderr')

    child.on('error', (e: NodeJS.ErrnoException) => {
      if (e.code === 'ENOENT') {
        reject(
          new CliError(`Python interpreter not found: ${cfg.pythonBin}`, {
            code: EXIT.CONFIG,
            hint: 'Set PYTHON_BIN in .env, or put python on PATH.',
          }),
        )
        return
      }
      reject(new CliError(`${cfg.pythonBin} failed: ${e.message}`, { code: EXIT.ERROR }))
    })

    child.on('close', (code) => resolve({ code: code ?? 0, stdout, stderr }))
  })

  return Object.assign(promise, { child })
}

function delimiter(): string {
  return process.platform === 'win32' ? ';' : ':'
}

/** Turn a flag map into argv, dropping undefined and rendering booleans as switches. */
export function toArgs(map: Record<string, unknown>): string[] {
  const args: string[] = []
  for (const [key, value] of Object.entries(map)) {
    if (value === undefined || value === null) continue
    const flag = `--${key}`
    if (typeof value === 'boolean') {
      if (value) args.push(flag)
      continue
    }
    args.push(flag, String(value))
  }
  return args
}
