import { spawn, type ChildProcess, type SpawnOptions } from 'node:child_process'

/**
 * Spawn a tool that may be a shell wrapper rather than an executable.
 *
 * On Windows the things this CLI shells out to (`pnpm`, `cloudflared`, a
 * `python` shim from the Store) are frequently `.cmd`/`.bat` files, which
 * CreateProcess refuses to run directly; they need `shell: true`. Node
 * deprecates passing an argv array together with `shell: true` (DEP0190)
 * because it concatenates without escaping, so the command line is built here
 * instead, quoting every argument once.
 */
export function spawnTool(bin: string, args: string[], opts: SpawnOptions): ChildProcess {
  if (process.platform !== 'win32') return spawn(bin, args, opts)
  const line = [bin, ...args].map(quote).join(' ')
  return spawn(line, { ...opts, shell: true })
}

/**
 * Quote one argument for cmd.exe. Anything without whitespace or a shell
 * metacharacter is already a single token and is left alone; the rest is
 * wrapped in double quotes with any embedded quote doubled.
 */
function quote(arg: string): string {
  if (arg !== '' && !/[\s"^&|<>()%!]/.test(arg)) return arg
  return `"${arg.replace(/"/g, '""')}"`
}
