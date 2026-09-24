import type { Command } from 'commander'

import { loadConfig, type Config } from './config.ts'
import { CliError, EXIT } from './errors.ts'
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

/**
 * Refuse a command that would spend on the Vast account from a machine that
 * does not own the fleet.
 *
 * `API_ORIGIN` is the whole test: when it is set, the autoscaler runs on that
 * deployment, and this process is a client of it. Renting, scaling or calling
 * the endpoint from here would put a second spender on one account, which is
 * how a machine that is merely shut down leaves a GPU billing. The reads stay
 * open - listing offers and instances costs nothing and is how you find out
 * what the far side is doing.
 *
 * Clearing `API_ORIGIN` in `.env` hands the fleet back to this machine, which
 * is the supported way to go back to the all-in-one local stack.
 */
export function assertOwnsFleet(cfg: Config, what: string): void {
  if (!cfg.apiOrigin) return
  throw new CliError(`${what} is disabled on this machine: the fleet lives at ${cfg.apiOrigin}.`, {
    code: EXIT.CONFIG,
    hint: `Run it where the autoscaler is, or clear API_ORIGIN in .env to drive Vast from here again.`,
  })
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
