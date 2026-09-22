import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

import { ENV_FILE, home } from './paths.ts'

/**
 * Port of `scripts/config.py:_read_dotenv`, including its quirks.
 *
 * Both halves matter and are deliberately not "improved":
 *  - values are stripped of one layer of matching or unmatched quotes exactly
 *    as the Python does (`.strip('"').strip("'")`);
 *  - the process environment overrides the file only for keys that are already
 *    present in the file, or whose name starts with VAST_ / S3_ / R2_. A key
 *    absent from `.env` and not so prefixed cannot be set from the environment.
 *    The CLI must resolve configuration the same way the Python engine does, or
 *    the two halves of one run would disagree.
 */
export function readDotenv(): Record<string, string> {
  const data: Record<string, string> = {}

  if (existsSync(ENV_FILE)) {
    const text = readFileSync(ENV_FILE, 'utf8')
    for (const raw of text.split(/\r?\n/)) {
      const line = raw.trim()
      if (!line || line.startsWith('#') || !line.includes('=')) continue
      const idx = line.indexOf('=')
      const k = line.slice(0, idx).trim()
      const v = line.slice(idx + 1).trim().replace(/^"|"$/g, '').replace(/^'|'$/g, '')
      data[k] = v
    }
  }

  for (const [k, v] of Object.entries(process.env)) {
    if (v === undefined) continue
    if (k in data || k.startsWith('VAST_') || k.startsWith('S3_') || k.startsWith('R2_')) {
      data[k] = v
    }
  }
  return data
}

/**
 * Port of `scripts/config.py:api_key`, same precedence:
 * `VAST_API_KEY` from the real environment, then from `.env`, then the two
 * files the vastai CLI writes.
 */
export function resolveApiKey(env: Record<string, string>): { key: string; source: string } | null {
  const fromProc = (process.env['VAST_API_KEY'] || '').trim()
  if (fromProc) return { key: fromProc, source: 'environment' }

  const fromFile = (env['VAST_API_KEY'] || '').trim()
  if (fromFile) return { key: fromFile, source: '.env' }

  const h = home()
  for (const candidate of [join(h, '.config', 'vastai', 'vast_api_key'), join(h, '.vast_api_key')]) {
    try {
      if (!existsSync(candidate)) continue
      const k = readFileSync(candidate, 'utf8').trim()
      if (k) return { key: k, source: candidate }
    } catch {
      // unreadable candidate is simply not a source
    }
  }
  return null
}
