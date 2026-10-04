import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, type ModelStats, type Bucket, type TraceDetail, type TraceSummary } from './api'
import { CostBars } from './CostBars'
import { count, pct, seconds, usd, when } from './format'
import { TimeBars } from './TimeBars'
import { Waterfall } from './Waterfall'

const RANGES = [
  { key: '1h', label: 'Last hour', seconds: 3600, bucket: 300 },
  { key: '24h', label: '24 hours', seconds: 24 * 3600, bucket: 3600 },
  { key: '7d', label: '7 days', seconds: 7 * 24 * 3600, bucket: 6 * 3600 },
  { key: '30d', label: '30 days', seconds: 30 * 24 * 3600, bucket: 24 * 3600 },
] as const

type RangeKey = (typeof RANGES)[number]['key']

export default function App() {
  const [rangeKey, setRangeKey] = useState<RangeKey>('24h')
  const [stats, setStats] = useState<ModelStats[]>([])
  const [buckets, setBuckets] = useState<Bucket[]>([])
  const [traces, setTraces] = useState<TraceSummary[]>([])
  const [detail, setDetail] = useState<TraceDetail | null>(null)
  const [fetchError, setFetchError] = useState<string | null>(null)
  const [since, setSince] = useState(() => Date.now() / 1000 - 24 * 3600)

  const range = RANGES.find((r) => r.key === rangeKey)!

  const refresh = useCallback(async () => {
    const sinceNow = Date.now() / 1000 - range.seconds
    setSince(sinceNow)
    try {
      const [s, b, t] = await Promise.all([
        api.modelStats(sinceNow),
        api.timeseries(sinceNow, range.bucket),
        api.traces(),
      ])
      setStats(s)
      setBuckets(b)
      setTraces(t.traces)
      setFetchError(null)
    } catch (e) {
      setFetchError(e instanceof Error ? e.message : String(e))
    }
  }, [range])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 15000)
    return () => clearInterval(id)
  }, [refresh])

  const totals = useMemo(() => {
    const calls = stats.reduce((n, s) => n + s.calls, 0)
    const errors = stats.reduce((n, s) => n + s.errors, 0)
    const cost = stats.reduce((n, s) => n + s.cost_usd, 0)
    const tokens = stats.reduce((n, s) => n + s.input_tokens + s.output_tokens, 0)
    const p95 = stats.length ? Math.max(...stats.map((s) => s.latency_p95)) : null
    return { calls, errors, cost, tokens, p95 }
  }, [stats])

  const openTrace = async (id: string) => {
    try {
      setDetail(await api.trace(id))
    } catch (e) {
      setFetchError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="shell">
      <header className="topbar">
        <h1>spanlight</h1>
        <span className="sub">self-hosted LLM observability</span>
      </header>

      {fetchError && (
        <div className="error-banner">
          {'⚠'} Could not reach the spanlight server: {fetchError}
        </div>
      )}

      {detail ? (
        <section className="card wide">
          <button className="backlink" onClick={() => setDetail(null)}>
            &larr; All traces
          </button>
          <h2>
            {detail.name}
            {detail.status === 'error' && <span className="err-text"> · error</span>}
          </h2>
          <div className="meta-line">
            <span>{when(detail.started_at)}</span>
            <span>
              {detail.ended_at != null
                ? seconds(detail.ended_at - detail.started_at)
                : 'still open'}
            </span>
            <span>{detail.spans.length} spans</span>
            <span>
              {usd(detail.spans.reduce((n, s) => n + (s.cost_usd ?? 0), 0))} ·{' '}
              {count(
                detail.spans.reduce(
                  (n, s) => n + (s.input_tokens ?? 0) + (s.output_tokens ?? 0),
                  0,
                ),
              )}{' '}
              tokens
            </span>
            {detail.session && <span>session: {detail.session}</span>}
          </div>
          {detail.spans.length ? (
            <Waterfall trace={detail} />
          ) : (
            <div className="empty">This trace has no spans.</div>
          )}
        </section>
      ) : (
        <>
          <div className="filters" role="radiogroup" aria-label="Time range">
            {RANGES.map((r) => (
              <button
                key={r.key}
                className={r.key === rangeKey ? 'on' : ''}
                onClick={() => setRangeKey(r.key)}
              >
                {r.label}
              </button>
            ))}
          </div>

          <div className="grid-tiles">
            <div className="tile">
              <div className="label">LLM calls</div>
              <div className="value">{count(totals.calls)}</div>
            </div>
            <div className="tile">
              <div className="label">Spend</div>
              <div className="value">{usd(totals.cost)}</div>
              <div className="hint">{count(totals.tokens)} tokens</div>
            </div>
            <div className="tile">
              <div className="label">Error rate</div>
              <div className={totals.errors > 0 ? 'value bad' : 'value'}>
                {totals.calls ? pct(totals.errors / totals.calls) : '-'}
              </div>
              <div className="hint">
                {totals.errors > 0 ? `⚠ ${totals.errors} failed` : 'no failures'}
              </div>
            </div>
            <div className="tile">
              <div className="label">Worst p95 latency</div>
              <div className="value">{seconds(totals.p95)}</div>
            </div>
          </div>

          <div className="cards">
            <section className="card">
              <h2>LLM calls over time</h2>
              <TimeBars
                buckets={buckets}
                since={since}
                bucketSeconds={range.bucket}
                metric="calls"
              />
            </section>
            <section className="card">
              <h2>Spend over time (USD)</h2>
              <TimeBars
                buckets={buckets}
                since={since}
                bucketSeconds={range.bucket}
                metric="cost"
              />
            </section>
            <section className="card wide">
              <h2>Models</h2>
              {stats.length ? (
                <>
                  <CostBars stats={stats} />
                  <table style={{ marginTop: 12 }}>
                    <thead>
                      <tr>
                        <th>Model</th>
                        <th className="num">Calls</th>
                        <th className="num">Errors</th>
                        <th className="num">Tokens in</th>
                        <th className="num">Tokens out</th>
                        <th className="num">Cost</th>
                        <th className="num">p50</th>
                        <th className="num">p95</th>
                      </tr>
                    </thead>
                    <tbody>
                      {stats.map((s) => (
                        <tr key={s.model}>
                          <td>{s.model}</td>
                          <td className="num">{count(s.calls)}</td>
                          <td className={s.errors ? 'num err-text' : 'num'}>
                            {s.errors ? `⚠ ${s.errors}` : '0'}
                          </td>
                          <td className="num">{count(s.input_tokens)}</td>
                          <td className="num">{count(s.output_tokens)}</td>
                          <td className="num">{usd(s.cost_usd)}</td>
                          <td className="num">{seconds(s.latency_p50)}</td>
                          <td className="num">{seconds(s.latency_p95)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </>
              ) : (
                <div className="empty">
                  No LLM calls in this range yet. Point an SDK at this server or run the demo
                  seeder.
                </div>
              )}
            </section>
            <section className="card wide">
              <h2>Recent traces</h2>
              {traces.length ? (
                <table>
                  <thead>
                    <tr>
                      <th>Trace</th>
                      <th>When</th>
                      <th className="num">Spans</th>
                      <th className="num">LLM</th>
                      <th className="num">Tokens</th>
                      <th className="num">Cost</th>
                      <th className="num">Duration</th>
                    </tr>
                  </thead>
                  <tbody>
                    {traces.map((t) => (
                      <tr key={t.id} className="click" onClick={() => openTrace(t.id)}>
                        <td>
                          <span className={`status-dot status-${t.status}`} />
                          {t.name}
                        </td>
                        <td>{when(t.started_at)}</td>
                        <td className="num">{t.span_count}</td>
                        <td className="num">{t.llm_calls}</td>
                        <td className="num">{count(t.input_tokens + t.output_tokens)}</td>
                        <td className="num">{usd(t.cost_usd)}</td>
                        <td className="num">
                          {t.ended_at != null ? seconds(t.ended_at - t.started_at) : 'open'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <div className="empty">No traces yet.</div>
              )}
            </section>
          </div>
        </>
      )}
    </div>
  )
}
