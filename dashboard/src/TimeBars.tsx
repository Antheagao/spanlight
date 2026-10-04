import { useMemo, useRef } from 'react'
import type { Bucket } from './api'
import { clock, count, usd } from './format'
import { Tip } from './Tip'
import { useTip } from './useTip'

interface Props {
  buckets: Bucket[]
  since: number
  /** The fetch timestamp, passed in so render stays pure. */
  now: number
  bucketSeconds: number
  metric: 'calls' | 'cost'
}

/** Bucketed bars over time. `calls` stacks ok under errors (status color,
 *  legend carries the warning icon + label); `cost` is a single series. */
export function TimeBars({ buckets, since, now, bucketSeconds, metric }: Props) {
  const ref = useRef<HTMLDivElement>(null)
  const { tip, show, hide } = useTip(ref)

  const dense = useMemo(() => {
    const byBucket = new Map(buckets.map((b) => [b.bucket, b]))
    const start = Math.floor(since / bucketSeconds) * bucketSeconds
    const end = Math.floor(now / bucketSeconds) * bucketSeconds
    const cols: Bucket[] = []
    for (let t = start; t <= end; t += bucketSeconds) {
      cols.push(byBucket.get(t) ?? { bucket: t, calls: 0, cost_usd: 0, errors: 0, tokens: 0 })
    }
    return cols
  }, [buckets, since, now, bucketSeconds])

  const max = Math.max(1e-9, ...dense.map((b) => (metric === 'calls' ? b.calls : b.cost_usd)))
  const fmt = metric === 'calls' ? (v: number) => count(Math.round(v)) : usd
  const range = now - since

  return (
    <div>
      <div className="timebars" ref={ref}>
        {[1, 0.5].map((f) => (
          <div key={f} className="gridline" style={{ top: `${(1 - f) * 100}%` }}>
            <span className="gridlabel">{fmt(max * f)}</span>
          </div>
        ))}
        <div className="cols">
          {dense.map((b) => {
            const total = metric === 'calls' ? b.calls : b.cost_usd
            const errs = metric === 'calls' ? b.errors : 0
            const ok = total - errs
            const label = (
              <>
                <b>{clock(b.bucket, range)}</b>
                <br />
                {metric === 'calls' ? `${b.calls} calls` : `${usd(b.cost_usd)} spent`}
                {errs > 0 && (
                  <>
                    <br />
                    {'⚠'} {errs} errors
                  </>
                )}
                {b.tokens > 0 && (
                  <>
                    <br />
                    {count(b.tokens)} tokens
                  </>
                )}
              </>
            )
            return (
              <div
                key={b.bucket}
                className="col"
                onMouseMove={(e) => show(e, label)}
                onMouseLeave={hide}
              >
                {errs > 0 && (
                  <div
                    className="seg top"
                    style={{ height: `${(errs / max) * 100}%`, background: 'var(--status-critical)' }}
                  />
                )}
                {ok > 0 && (
                  <div
                    className={errs > 0 ? 'seg' : 'seg top'}
                    style={{ height: `${(ok / max) * 100}%`, background: 'var(--series-1)' }}
                  />
                )}
              </div>
            )
          })}
        </div>
        <Tip tip={tip} />
      </div>
      <div className="xaxis">
        <span>{clock(since, range)}</span>
        <span>now</span>
      </div>
      {metric === 'calls' && (
        <div className="legend">
          <span className="chip">
            <span className="swatch" style={{ background: 'var(--series-1)' }} /> ok
          </span>
          <span className="chip">
            <span className="swatch" style={{ background: 'var(--status-critical)' }} />
            {'⚠'} errors
          </span>
        </div>
      )}
    </div>
  )
}
