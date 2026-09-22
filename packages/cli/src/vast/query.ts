import { CliError, EXIT } from '../lib/errors.ts'

/**
 * Port of the Vast CLI query-string parser, restricted to offer searches.
 *
 * `VAST_SEARCH_PARAMS` in .env is written in exactly this syntax
 * ("verified=true gpu_ram>=16 dph_total<=0.15"), and it is the same string the
 * workergroup stores, so the CLI has to turn it into the same query object the
 * autoscaler ends up with. Notably `gpu_ram` is given in GB and sent in MB;
 * getting that multiplier wrong silently searches for 16 MB cards.
 */
const OP_NAMES: Record<string, string> = {
  '>=': 'gte',
  '>': 'gt',
  gt: 'gt',
  gte: 'gte',
  '<=': 'lte',
  '<': 'lt',
  lt: 'lt',
  lte: 'lte',
  '!=': 'neq',
  '==': 'eq',
  '=': 'eq',
  eq: 'eq',
  neq: 'neq',
  noteq: 'neq',
  'not eq': 'neq',
  notin: 'notin',
  'not in': 'notin',
  nin: 'notin',
  in: 'in',
}

/** Field multipliers from the Vast CLI's `offers_mult`. */
const FIELD_MULTIPLIER: Record<string, number> = {
  cpu_ram: 1000,
  gpu_ram: 1000,
  gpu_total_ram: 1000,
  duration: 24 * 60 * 60,
}

/** Field aliases from the Vast CLI's `offers_alias`. */
const FIELD_ALIAS: Record<string, string> = {
  cuda_vers: 'cuda_max_good',
  display_active: 'gpu_display_active',
  dlperf_usd: 'dlperf_per_dphtotal',
  dph: 'dph_total',
  flops_usd: 'flops_per_dphtotal',
}

export type QueryValue = string | number | boolean | null | string[]
export type Query = Record<string, Record<string, QueryValue>>

const PATTERN =
  /([a-zA-Z0-9_]+)( *[=><!]+| +(?:[lg]te?|nin|neq|eq|not ?eq|not ?in|in) )?( *)(\[[^\]]+\]|"[^"]+"|[^ ]+)?( *)/g

export function parseQuery(queryStr: string, into: Query = {}): Query {
  const res: Query = into
  const text = (queryStr ?? '').trim()
  if (!text) return res

  PATTERN.lastIndex = 0
  const matches = [...text.matchAll(PATTERN)]

  // The Python raises on leftovers rather than quietly dropping a filter, and
  // so does this: a typo in VAST_SEARCH_PARAMS must not become a wider search.
  const joined = matches.map((m) => m.slice(1, 6).map((x) => x ?? '').join('')).join('')
  if (joined !== text) {
    throw new CliError(
      `Could not parse the search query. Unconsumed text: ${JSON.stringify(text.slice(joined.length))}`,
      { code: EXIT.USAGE, hint: 'Quote values containing spaces, e.g. gpu_name="RTX 4090".' },
    )
  }

  for (const m of matches) {
    let field = m[1] ?? ''
    const op = (m[2] ?? '').trim()
    let value: string = (m[4] ?? '').replace(/^[,[]+|[,\]]+$/g, '')

    if (!field) continue
    const opName = OP_NAMES[op]
    if (!opName) {
      throw new CliError(`Unknown operator ${JSON.stringify(op)} in the search query near ${JSON.stringify(field)}.`, {
        code: EXIT.USAGE,
      })
    }
    if (!value) {
      throw new CliError(`Blank value for ${JSON.stringify(field)} in the search query.`, { code: EXIT.USAGE })
    }

    const alias = FIELD_ALIAS[field]
    if (alias) {
      const previous = res[field]
      if (previous) {
        delete res[field]
        res[alias] = { ...(res[alias] ?? {}), ...previous }
      }
      field = alias
    }

    const bucket = res[field] ?? {}

    if (opName === 'in' || opName === 'notin') {
      bucket[opName] = value
        .split(',')
        .map((x) => x.trim().replace(/_/g, ' ').replace(/^"|"$/g, ''))
        .filter(Boolean)
    } else {
      value = value.replace(/_/g, ' ').replace(/^"|"$/g, '')
      const mult = FIELD_MULTIPLIER[field]
      if (mult !== undefined) {
        const n = Number(value)
        if (!Number.isFinite(n)) {
          throw new CliError(`${field} expects a number, got ${JSON.stringify(value)}.`, { code: EXIT.USAGE })
        }
        bucket[opName] = n * mult
      } else if (value === 'true' || value === 'True') {
        bucket[opName] = true
      } else if (value === 'false' || value === 'False') {
        bucket[opName] = false
      } else if (value === 'None' || value === 'null') {
        bucket[opName] = null
      } else {
        bucket[opName] = value
      }
    }

    res[field] = bucket
  }

  return res
}
