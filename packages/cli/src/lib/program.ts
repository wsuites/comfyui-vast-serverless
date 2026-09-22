import type { Command } from 'commander'

import { loadConfig, type Config } from './config.ts'
import { setColour } from './output.ts'

export interface CommonOptions {
  json?: boolean
  color?: boolean
  yes?: boolean
}

/**
 * Attach the options every command shares.
 *
 * `--json` is per-command rather than global because commander hands options
 * that follow a subcommand to that subcommand; declaring it only on the root
 * would make `cv instance ls --json` a parse error.
 */
export function common(cmd: Command): Command {
  return cmd
    .option('--json', 'machine-readable output on stdout')
    .option('--no-color', 'disable colour even on a TTY')
}

/** Adds the confirmation bypass for destructive commands. */
export function destructive(cmd: Command): Command {
  return common(cmd).option('-y, --yes', 'skip the confirmation prompt')
}

export interface Ctx {
  cfg: Config
  json: boolean
  yes: boolean
}

/**
 * Per-invocation context. Colour is settled here, once, so that --json output
 * never carries escapes regardless of what the terminal supports.
 */
export function ctx(opts: CommonOptions): Ctx {
  const json = opts.json === true
  if (json || opts.color === false) setColour(false)
  return { cfg: loadConfig(), json, yes: opts.yes === true }
}

/** Parse a numeric option, failing loudly instead of silently becoming NaN. */
export function int(value: string, name: string): number {
  const n = Number(value)
  if (!Number.isFinite(n)) throw new TypeError(`${name} must be a number, got ${JSON.stringify(value)}`)
  return Math.trunc(n)
}

export function float(value: string, name: string): number {
  const n = Number(value)
  if (!Number.isFinite(n)) throw new TypeError(`${name} must be a number, got ${JSON.stringify(value)}`)
  return n
}
