#!/usr/bin/env node
// Thin shim: the real program lives in dist/, built by tsup.
import { existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const entry = join(here, '..', 'dist', 'cv.js')

if (!existsSync(entry)) {
  process.stderr.write(
    'cv: the CLI is not built yet.\n' +
    '    Run: pnpm --filter @comfy-vast/cli build\n',
  )
  process.exit(3)
}

await import(pathToFileURL(entry).href)
