import { readFile, readdir, stat } from 'node:fs/promises'
import { join, relative, sep } from 'node:path'

const IMAGE_EXT = new Set(['.png', '.jpg', '.jpeg', '.webp'])

export interface RunInfo {
  command?: string
  argv?: string[]
  cwd?: string
  extra?: Record<string, unknown>
}

export interface GalleryImage {
  /** Absolute path. */
  path: string
  /** Path relative to GALLERY_DIR, with forward slashes. */
  rel: string
  /** Directory relative to GALLERY_DIR; the unit a run_info.json covers. */
  group: string
  size: number
  mtime: number
  /** Provenance from the nearest run_info.json at or above the image. */
  run: RunInfo | null
  runFile: string | null
}

/**
 * Walk GALLERY_DIR collecting images and the provenance that explains them.
 *
 * `scripts/config.py:registrar_run` writes one run_info.json per output folder,
 * so provenance is resolved by walking up from each image to the nearest one
 * rather than assuming it sits in the same directory: the A/B scripts nest a
 * level deeper (output/ab-model/<arm>/...).
 */
export async function scanGallery(
  root: string,
  opts: { limit?: number; match?: string; group?: string } = {},
): Promise<GalleryImage[]> {
  const images: GalleryImage[] = []
  const runCache = new Map<string, { info: RunInfo; file: string } | null>()

  async function runFor(dir: string): Promise<{ info: RunInfo; file: string } | null> {
    if (runCache.has(dir)) return runCache.get(dir) ?? null
    let found: { info: RunInfo; file: string } | null = null
    const candidate = join(dir, 'run_info.json')
    try {
      const text = await readFile(candidate, 'utf8')
      found = { info: JSON.parse(text) as RunInfo, file: candidate }
    } catch {
      // No provenance here. Keep walking up, but never above the gallery root.
      if (dir !== root && dir.startsWith(root)) {
        const up = dir.slice(0, dir.lastIndexOf(sep))
        if (up && up.length >= root.length) found = await runFor(up)
      }
    }
    runCache.set(dir, found)
    return found
  }

  async function walk(dir: string, depth: number): Promise<void> {
    if (depth > 8) return
    let entries
    try {
      entries = await readdir(dir, { withFileTypes: true })
    } catch {
      return
    }
    for (const e of entries) {
      const full = join(dir, e.name)
      if (e.isDirectory()) {
        if (e.name === 'node_modules' || e.name.startsWith('.')) continue
        await walk(full, depth + 1)
        continue
      }
      if (!e.isFile()) continue
      const dot = e.name.lastIndexOf('.')
      if (dot < 0 || !IMAGE_EXT.has(e.name.slice(dot).toLowerCase())) continue

      const rel = relative(root, full).split(sep).join('/')
      if (opts.match && !rel.toLowerCase().includes(opts.match.toLowerCase())) continue
      const group = relative(root, dir).split(sep).join('/') || '.'
      if (opts.group && group !== opts.group && !group.startsWith(opts.group + '/')) continue

      const st = await stat(full)
      const run = await runFor(dir)
      images.push({
        path: full,
        rel,
        group,
        size: st.size,
        mtime: st.mtimeMs,
        run: run?.info ?? null,
        runFile: run?.file ?? null,
      })
    }
  }

  await walk(root, 0)
  images.sort((a, b) => b.mtime - a.mtime)
  return opts.limit ? images.slice(0, opts.limit) : images
}

/** One-line summary of what produced an image, for a table cell. */
export function runSummary(run: RunInfo | null): string {
  if (!run) return ''
  const cmd = run.command ?? ''
  const script = cmd.split(/\s+/).find((t) => t.endsWith('.py'))
  const argv = (run.argv ?? []).join(' ')
  const head = script ? script.split('/').pop() ?? script : 'run'
  return argv ? `${head} ${argv}` : head
}
