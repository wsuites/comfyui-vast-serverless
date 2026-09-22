import type { VastClient } from './client.ts'
import { parseQuery, type Query } from './query.ts'
import type { VastOffer } from './types.ts'

export interface SearchOptions {
  /** Extra query text appended to the baseline, same syntax as VAST_SEARCH_PARAMS. */
  extra?: string
  /** Baseline query text; normally VAST_SEARCH_PARAMS from .env. */
  baseline?: string
  order?: [string, 'asc' | 'desc'][]
  limit?: number
  /** Allocated storage in GiB, which is what storage_cost is priced against. */
  storage?: number
  type?: 'on-demand' | 'reserved' | 'bid'
  /** Skip the verified/external/rentable/rented defaults. */
  noDefault?: boolean
}

export interface BuiltSearch {
  query: Record<string, unknown>
  filters: Query
}

export function buildSearch(opts: SearchOptions): BuiltSearch {
  const base: Query = opts.noDefault
    ? {}
    : {
        verified: { eq: true },
        external: { eq: false },
        rentable: { eq: true },
        rented: { eq: false },
      }

  // The baseline goes in first so an explicit --filter on the command line can
  // override the same field coming from .env.
  const filters = parseQuery(opts.extra ?? '', parseQuery(opts.baseline ?? '', base))

  const q: Record<string, unknown> = { ...filters }
  q['order'] = opts.order ?? [['dph_total', 'asc']]
  q['type'] = opts.type ?? 'on-demand'
  if (opts.limit) q['limit'] = Math.trunc(opts.limit)
  q['allocated_storage'] = opts.storage ?? 5.0

  return { query: q, filters }
}

/**
 * Offer search.
 *
 * `select_cols` is deliberately omitted: the newer /search/asks/ route rejects
 * the `["*"]` the old SDK sends, and without it the backend returns full rows.
 */
export async function searchOffers(client: VastClient, opts: SearchOptions): Promise<VastOffer[]> {
  const { query } = buildSearch(opts)
  const res = await client.put<{ offers?: VastOffer[] }>('/search/asks/', { q: query })
  return res.offers ?? []
}
