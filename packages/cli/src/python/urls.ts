/**
 * Port of `scripts/vast_state.py:extract_image_urls`.
 *
 * The worker wraps its real answer as a JSON *string* inside the envelope, so
 * a plain object walk misses the output list entirely; embedded JSON has to be
 * parsed as it is encountered. The extension filter is what keeps the
 * envelope's own `url` (the worker endpoint) from being mistaken for a result.
 */
const IMAGE_SUFFIX = ['.png', '.jpg', '.jpeg', '.webp']

function* walk(node: unknown): Generator<Record<string, unknown>> {
  if (typeof node === 'string') {
    const trimmed = node.trim()
    if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
      try {
        yield* walk(JSON.parse(trimmed))
      } catch {
        // not JSON after all
      }
    }
    return
  }
  if (Array.isArray(node)) {
    for (const item of node) yield* walk(item)
    return
  }
  if (node && typeof node === 'object') {
    const obj = node as Record<string, unknown>
    yield obj
    for (const value of Object.values(obj)) yield* walk(value)
  }
}

export function extractImageUrls(result: unknown): string[] {
  const urls: string[] = []
  for (const entry of walk(result)) {
    const url = entry['url']
    if (typeof url !== 'string' || !url.startsWith('http')) continue
    const name = url.split('?', 1)[0]?.toLowerCase() ?? ''
    if (IMAGE_SUFFIX.some((s) => name.endsWith(s)) && !urls.includes(url)) urls.push(url)
  }
  return urls
}
