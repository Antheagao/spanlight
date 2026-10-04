export interface TraceSummary {
  id: string
  name: string
  started_at: number
  ended_at: number | null
  status: 'ok' | 'error'
  session: string | null
  metadata: Record<string, unknown>
  span_count: number
  llm_calls: number
  input_tokens: number
  output_tokens: number
  cost_usd: number
}

export interface Span {
  id: string
  trace_id: string
  parent_id: string | null
  name: string
  kind: 'llm' | 'retrieval' | 'tool' | 'other'
  started_at: number
  ended_at: number | null
  status: 'ok' | 'error'
  error: string | null
  model: string | null
  input_tokens: number | null
  output_tokens: number | null
  cost_usd: number | null
  input: string | null
  output: string | null
  attributes: Record<string, unknown>
}

export interface TraceDetail extends Omit<TraceSummary, 'span_count' | 'llm_calls' | 'input_tokens' | 'output_tokens' | 'cost_usd'> {
  spans: Span[]
}

export interface ModelStats {
  model: string
  calls: number
  errors: number
  input_tokens: number
  output_tokens: number
  cost_usd: number
  latency_p50: number
  latency_p95: number
}

export interface Bucket {
  bucket: number
  calls: number
  cost_usd: number
  errors: number
  tokens: number
}

async function get<T>(url: string): Promise<T> {
  const r = await fetch(url)
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`)
  return r.json() as Promise<T>
}

export const api = {
  traces: (q?: string) =>
    get<{ traces: TraceSummary[]; next_before: number | null }>(
      '/api/traces?limit=50' + (q ? `&q=${encodeURIComponent(q)}` : ''),
    ),
  trace: (id: string) => get<TraceDetail>(`/api/traces/${id}`),
  modelStats: (since: number) => get<ModelStats[]>(`/api/stats/models?since=${since}`),
  timeseries: (since: number, bucketSeconds: number) =>
    get<Bucket[]>(`/api/stats/timeseries?since=${since}&bucket_seconds=${bucketSeconds}`),
}
