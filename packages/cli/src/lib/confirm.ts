import { confirm as clackConfirm, isCancel, text as clackText } from '@clack/prompts'

import { Cancelled, CliError, EXIT } from './errors.ts'
import { c, err, isTTY } from './output.ts'

export interface ConfirmOptions {
  /** --yes was passed: never prompt. */
  yes?: boolean
  message: string
  /** Extra lines shown above the question, e.g. what is about to be destroyed. */
  detail?: string[]
}

/**
 * Gate for a destructive operation.
 *
 * Without a TTY there is nobody to ask, so the command fails rather than
 * guessing; --yes is the non-interactive path.
 */
export async function confirmDestructive(o: ConfirmOptions): Promise<void> {
  if (o.yes) return

  if (!isTTY() || !process.stdin.isTTY) {
    throw new CliError(`${o.message} Refusing to do it without confirmation.`, {
      code: EXIT.USAGE,
      hint: 'Re-run with --yes to confirm non-interactively.',
    })
  }

  for (const line of o.detail ?? []) err(c.dim('  ' + line))

  const answer = await clackConfirm({ message: o.message, initialValue: false })
  if (isCancel(answer) || answer !== true) throw new Cancelled()
}

export async function askText(message: string, placeholder?: string): Promise<string> {
  if (!isTTY() || !process.stdin.isTTY) {
    throw new CliError('This command needs an interactive terminal.', { code: EXIT.USAGE })
  }
  const opts: { message: string; placeholder?: string } = { message }
  if (placeholder !== undefined) opts.placeholder = placeholder
  const answer = await clackText(opts)
  if (isCancel(answer)) throw new Cancelled()
  return String(answer)
}

export async function askYesNo(message: string, initial = false): Promise<boolean> {
  if (!isTTY() || !process.stdin.isTTY) return false
  const answer = await clackConfirm({ message, initialValue: initial })
  if (isCancel(answer)) throw new Cancelled()
  return answer === true
}
