import { randomInt } from 'node:crypto'
import { existsSync, readFileSync, writeFileSync } from 'node:fs'

import { CliError, EXIT } from '../lib/errors.ts'
import { ENV_FILE } from '../lib/paths.ts'

// Ambiguous glyphs removed: this password gets read off a terminal and typed
// into a browser prompt, sometimes on a phone.
const ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'

export function generatePassword(length = 24): string {
  let out = ''
  for (let i = 0; i < length; i++) out += ALPHABET[randomInt(ALPHABET.length)]
  return out
}

/**
 * Set one key in the repository `.env`, in place.
 *
 * Rewrites the existing line if the key is there (keeping its position and the
 * comments around it) and appends otherwise. Only ever called after the
 * operator has said yes: `.env` is not versioned and holds live credentials.
 */
export function writeEnvKey(key: string, value: string): void {
  if (!existsSync(ENV_FILE)) {
    throw new CliError(`No .env at ${ENV_FILE}.`, {
      code: EXIT.CONFIG,
      hint: 'Copy .env.example to .env first.',
    })
  }

  const original = readFileSync(ENV_FILE, 'utf8')
  const eol = original.includes('\r\n') ? '\r\n' : '\n'
  const lines = original.split(/\r?\n/)

  let replaced = false
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i] ?? ''
    const trimmed = line.trim()
    if (trimmed.startsWith('#') || !trimmed.includes('=')) continue
    if (trimmed.slice(0, trimmed.indexOf('=')).trim() !== key) continue
    lines[i] = `${key}=${value}`
    replaced = true
    break
  }

  if (!replaced) {
    if (lines.length && lines[lines.length - 1] !== '') lines.push('')
    lines.push(`${key}=${value}`)
    lines.push('')
  }

  writeFileSync(ENV_FILE, lines.join(eol), 'utf8')
}
