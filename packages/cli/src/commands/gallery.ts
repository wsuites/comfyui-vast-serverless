import { spawn } from 'node:child_process'
import { createReadStream } from 'node:fs'
import { stat } from 'node:fs/promises'
import { createServer } from 'node:http'
import { extname, join, normalize, resolve, sep } from 'node:path'

import { Command } from 'commander'

import { galleryRoot } from '../lib/config.ts'
import { CliError, EXIT } from '../lib/errors.ts'
import { c, fmtBytes, json, out, printTable } from '../lib/output.ts'
import { scanGallery, runSummary, type GalleryImage } from '../lib/gallery.ts'
import { common, ctx, int, type CommonOptions } from '../lib/program.ts'

const MIME: Record<string, string> = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.json': 'application/json',
}

function when(mtime: number): string {
  return new Date(mtime).toISOString().slice(0, 19).replace('T', ' ')
}

function plain(img: GalleryImage): Record<string, unknown> {
  return {
    path: img.path,
    rel: img.rel,
    group: img.group,
    size: img.size,
    mtime: new Date(img.mtime).toISOString(),
    run: img.run,
    run_file: img.runFile,
  }
}

/** Open a file with whatever the platform uses; nothing here is portable. */
function openNative(target: string): void {
  const [cmd, args] =
    process.platform === 'win32'
      ? ['cmd', ['/c', 'start', '', target]]
      : process.platform === 'darwin'
        ? ['open', [target]]
        : ['xdg-open', [target]]
  const child = spawn(cmd as string, args as string[], { detached: true, stdio: 'ignore' })
  child.unref()
}

async function findOne(root: string, needle: string): Promise<GalleryImage> {
  const images = await scanGallery(root, { match: needle })
  const exact = images.find((i) => i.rel === needle || i.path === resolve(needle))
  const hit = exact ?? images[0]
  if (!hit) {
    throw new CliError(`No image in the gallery matches ${JSON.stringify(needle)}.`, {
      code: EXIT.NOT_FOUND,
      hint: 'cv gallery ls',
    })
  }
  return hit
}

export function galleryCommand(): Command {
  const cmd = new Command('gallery').description('browse the rendered images under GALLERY_DIR')

  common(cmd.command('ls', { isDefault: true }))
    .description('most recent images first, with what produced them')
    .option('-n, --limit <n>', 'how many to list', (v) => int(v, '--limit'), 30)
    .option('-m, --match <text>', 'only paths containing this')
    .option('-g, --group <dir>', 'only this subdirectory of GALLERY_DIR')
    .option('--runs', 'group by run instead of listing every image')
    .action(async (o: CommonOptions & { limit: number; match?: string; group?: string; runs?: boolean }) => {
      const { cfg, json: asJson } = ctx(o)
      const root = galleryRoot(cfg)

      const scan: { limit?: number; match?: string; group?: string } = {}
      if (o.match !== undefined) scan.match = o.match
      if (o.group !== undefined) scan.group = o.group
      const all = await scanGallery(root, scan)

      if (o.runs) {
        const groups = new Map<string, GalleryImage[]>()
        for (const img of all) {
          const list = groups.get(img.group)
          if (list) list.push(img)
          else groups.set(img.group, [img])
        }
        const rows = [...groups.entries()].slice(0, o.limit)
        if (asJson) {
          return json(
            rows.map(([group, imgs]) => ({
              group,
              images: imgs.length,
              bytes: imgs.reduce((n, i) => n + i.size, 0),
              newest: new Date(Math.max(...imgs.map((i) => i.mtime))).toISOString(),
              run: imgs[0]?.run ?? null,
            })),
          )
        }
        printTable(
          ['WHEN', 'GROUP', 'N', 'SIZE', 'RUN'],
          rows.map(([group, imgs]) => [
            when(Math.max(...imgs.map((i) => i.mtime))),
            group,
            imgs.length,
            fmtBytes(imgs.reduce((n, i) => n + i.size, 0)),
            runSummary(imgs[0]?.run ?? null).slice(0, 60),
          ]),
          `no images under ${root}`,
        )
        return
      }

      const shown = all.slice(0, o.limit)
      if (asJson) return json(shown.map(plain))
      printTable(
        ['WHEN', 'PATH', 'SIZE', 'RUN'],
        shown.map((i) => [when(i.mtime), i.rel, fmtBytes(i.size), runSummary(i.run).slice(0, 50)]),
        `no images under ${root}`,
      )
      if (shown.length && all.length > shown.length) {
        out(c.dim(`${all.length - shown.length} more; raise --limit`))
      }
    })

  common(cmd.command('show <image>'))
    .description('one image with its full provenance')
    .action(async (needle: string, o: CommonOptions) => {
      const { cfg, json: asJson } = ctx(o)
      const img = await findOne(galleryRoot(cfg), needle)
      if (asJson) return json(plain(img))

      out(c.bold(img.rel))
      printTable(
        ['FIELD', 'VALUE'],
        [
          ['path', img.path],
          ['group', img.group],
          ['size', fmtBytes(img.size)],
          ['modified', when(img.mtime)],
          ['run_info', img.runFile ?? c.dim('none')],
        ],
      )
      if (!img.run) {
        out('')
        out(c.dim('No run_info.json covers this image, so there is no record of how it was made.'))
        return
      }
      out('')
      out(c.bold('provenance'))
      if (img.run.command) out(`  ${c.dim('command')} ${img.run.command}`)
      if (img.run.cwd) out(`  ${c.dim('cwd    ')} ${img.run.cwd}`)
      if (img.run.argv?.length) out(`  ${c.dim('argv   ')} ${img.run.argv.join(' ')}`)
      if (img.run.extra && Object.keys(img.run.extra).length) {
        out('')
        out(c.bold('parameters'))
        printTable(
          ['KEY', 'VALUE'],
          Object.entries(img.run.extra).map(([k, v]) => [
            k,
            typeof v === 'object' && v !== null ? JSON.stringify(v).slice(0, 120) : String(v),
          ]),
        )
      }
    })

  common(cmd.command('open [image]'))
    .description('open an image, or the gallery directory, in the desktop viewer')
    .action(async (needle: string | undefined, o: CommonOptions) => {
      const { cfg, json: asJson } = ctx(o)
      const root = galleryRoot(cfg)
      const target = needle ? (await findOne(root, needle)).path : root
      openNative(target)
      if (asJson) return json({ opened: target })
      out(c.dim(`opened ${target}`))
    })

  common(cmd.command('serve'))
    .description('serve the gallery over HTTP for a browser, including a phone')
    .option('-p, --port <n>', 'port to listen on', (v) => int(v, '--port'), 4330)
    .option('--host <addr>', 'address to bind', '127.0.0.1')
    .action(async (o: CommonOptions & { port: number; host: string }) => {
      const { cfg, json: asJson } = ctx(o)
      const root = galleryRoot(cfg)

      const server = createServer((req, res) => {
        void (async () => {
          const url = new URL(req.url ?? '/', 'http://localhost')
          const path = decodeURIComponent(url.pathname)

          if (path === '/' || path === '/index.html') {
            const images = await scanGallery(root, { limit: 500 })
            res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
            res.end(page(images))
            return
          }
          if (path === '/api/images') {
            const images = await scanGallery(root, { limit: 2000 })
            res.writeHead(200, { 'content-type': 'application/json' })
            res.end(JSON.stringify(images.map(plain)))
            return
          }

          // Everything else is a file under the gallery root. Resolve and
          // re-check the prefix: without this, /../../.env is served.
          const file = resolve(root, '.' + normalize(path).replace(/^[\\/]+/, '/'))
          if (file !== root && !file.startsWith(root + sep)) {
            res.writeHead(403).end('forbidden')
            return
          }
          try {
            const st = await stat(file)
            if (!st.isFile()) throw new Error('not a file')
            res.writeHead(200, {
              'content-type': MIME[extname(file).toLowerCase()] ?? 'application/octet-stream',
              'content-length': String(st.size),
              'cache-control': 'no-cache',
            })
            createReadStream(file).pipe(res)
          } catch {
            res.writeHead(404).end('not found')
          }
        })().catch(() => {
          if (!res.headersSent) res.writeHead(500).end('error')
        })
      })

      await new Promise<void>((ok, fail) => {
        server.once('error', fail)
        server.listen(o.port, o.host, ok)
      })

      const url = `http://${o.host}:${o.port}`
      if (asJson) json({ url, root })
      else {
        out(c.green(`gallery on ${c.bold(url)}`))
        out(c.dim(`serving ${root} — Ctrl-C to stop`))
      }

      await new Promise<void>((done) => {
        process.once('SIGINT', () => {
          server.close(() => done())
        })
      })
    })

  return cmd
}

/**
 * The viewer page.
 *
 * Self-contained and dependency-free on purpose: this is the fallback for when
 * the Astro UI is not built, so it cannot rely on anything the build produces.
 */
function page(images: GalleryImage[]): string {
  const cards = images
    .map((i) => {
      const src = '/' + i.rel.split('/').map(encodeURIComponent).join('/')
      const meta = `${when(i.mtime)} · ${fmtBytes(i.size)}`
      const run = runSummary(i.run)
      return `<figure><a href="${src}" target="_blank"><img loading="lazy" src="${src}" alt=""></a>
<figcaption><b>${escapeHtml(i.rel)}</b><span>${escapeHtml(meta)}</span>${
        run ? `<span class="run">${escapeHtml(run)}</span>` : ''
      }</figcaption></figure>`
    })
    .join('\n')

  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>cv gallery</title>
<style>
:root{color-scheme:dark}
body{margin:0;padding:16px;background:#0e0e11;color:#e7e7ea;font:14px/1.45 ui-sans-serif,system-ui,sans-serif}
h1{font-size:15px;font-weight:600;margin:0 0 16px;color:#9aa0a6}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(240px,1fr))}
figure{margin:0;background:#17171c;border-radius:10px;overflow:hidden}
img{display:block;width:100%;height:auto;background:#000}
figcaption{padding:8px 10px;display:flex;flex-direction:column;gap:2px;font-size:12px}
figcaption b{font-weight:500;word-break:break-all}
figcaption span{color:#8b9096}
.run{font-family:ui-monospace,monospace;font-size:11px;color:#6f757b;word-break:break-all}
p.empty{color:#8b9096}
</style></head>
<body><h1>cv gallery — ${images.length} image(s)</h1>
${images.length ? `<div class="grid">${cards}</div>` : '<p class="empty">Nothing rendered yet.</p>'}
</body></html>`
}

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch] as string)
}
