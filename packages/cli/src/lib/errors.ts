/** Process exit codes. Kept in one place so every command agrees. */
export const EXIT = {
  OK: 0,
  ERROR: 1,
  USAGE: 2,
  CONFIG: 3,
  UPSTREAM: 4,
  NOT_FOUND: 5,
  CANCELLED: 130,
} as const

export type ExitCode = (typeof EXIT)[keyof typeof EXIT]

/** An error with a chosen exit code and, optionally, a next step for the operator. */
export class CliError extends Error {
  readonly code: ExitCode
  readonly hint: string | undefined
  readonly details: unknown

  constructor(message: string, opts: { code?: ExitCode; hint?: string; details?: unknown } = {}) {
    super(message)
    this.name = 'CliError'
    this.code = opts.code ?? EXIT.ERROR
    this.hint = opts.hint
    this.details = opts.details
  }
}

/** Raised when the operator answers "no" to a confirmation prompt. */
export class Cancelled extends CliError {
  constructor(message = 'Cancelled') {
    super(message, { code: EXIT.CANCELLED })
    this.name = 'Cancelled'
  }
}
