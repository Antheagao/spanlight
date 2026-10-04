import { useMemo, useRef, useState } from 'react'
import type { Span, TraceDetail } from './api'
import { count, seconds, usd } from './format'
import { Tip, useTip } from './Tip'

const KIND_COLOR: Record<Span['kind'], string> = {
  llm: 'var(--series-1)',
  retrieval: 'var(--series-2)',
  tool: 'var(--series-3)',
  other: 'var(--baseline)',
}

interface Row {
  span: Span
  depth: number
}

/** Depth-first span order: roots by start time, children nested under
 *  their parent, also by start time. */
function flatten(spans: Span[]): Row[] {
  const children = new Map<string | null, Span[]>()
  for (const s of spans) {
    const key = s.parent_id && spans.some((p) => p.id === s.parent_id) ? s.parent_id : null
    children.set(key, [...(children.get(key) ?? []), s])
  }
  const rows: Row[] = []
  const visit = (parent: string | null, depth: number) => {
    for (const s of (children.get(parent) ?? []).sort((a, b) => a.started_at - b.started_at)) {
      rows.push({ span: s, depth })
      visit(s.id, depth + 1)
    }
  }
  visit(null, 0)
  return rows
}

export function Waterfall({ trace }: { trace: TraceDetail }) {
  const ref = useRef<HTMLDivElement>(null)
  const { tip, show, hide } = useTip(ref)
  const [selected, setSelected] = useState<Span | null>(null)

  const rows = useMemo(() => flatten(trace.spans), [trace])
  const t0 = Math.min(trace.started_at, ...trace.spans.map((s) => s.started_at))
  const t1 = Math.max(
    trace.ended_at ?? trace.started_at,
    ...trace.spans.map((s) => s.ended_at ?? s.started_at),
  )
  const total = Math.max(t1 - t0, 1e-9)
  const kindsPresent = [...new Set(rows.map((r) => r.span.kind))]
  const hasErrors = rows.some((r) => r.span.status === 'error')

  return (
    <div ref={ref} style={{ position: 'relative' }}>
      <div className="wf">
        {rows.map(({ span, depth }) => {
          const left = ((span.started_at - t0) / total) * 100
          const dur = span.ended_at != null ? span.ended_at - span.started_at : null
          const width = dur != null ? Math.max((dur / total) * 100, 0.5) : 0.5
          const labelAfter = left + width < 72
          const isError = span.status === 'error'
          const tipNode = (
            <>
              <b>
                {isError ? '⚠ ' : ''}
                {span.name}
              </b>
              <br />
              {span.kind}
              {span.model ? ` · ${span.model}` : ''} · {seconds(dur)}
              {span.input_tokens != null && (
                <>
                  <br />
                  {count(span.input_tokens)} in / {count(span.output_tokens)} out ·{' '}
                  {usd(span.cost_usd)}
                </>
              )}
              {isError && span.error && (
                <>
                  <br />
                  {span.error.slice(0, 80)}
                </>
              )}
            </>
          )
          return (
            <div key={span.id} className="wrow">
              <span
                className="wname"
                style={{ paddingLeft: depth * 14 }}
                title={span.name}
              >
                {isError && <span className="err-text">{'⚠'} </span>}
                {span.name}
              </span>
              <div
                className="wtrack"
                onMouseMove={(e) => show(e, tipNode)}
                onMouseLeave={hide}
                onClick={() => setSelected(selected?.id === span.id ? null : span)}
                style={{ cursor: 'pointer' }}
              >
                <span
                  className="wbar"
                  style={{
                    left: `${left}%`,
                    width: `${width}%`,
                    background: isError ? 'var(--status-critical)' : KIND_COLOR[span.kind],
                  }}
                />
                <span
                  className="wdur"
                  style={
                    labelAfter
                      ? { left: `calc(${left + width}% + 6px)` }
                      : { right: `calc(${100 - left}% + 6px)` }
                  }
                >
                  {seconds(dur)}
                </span>
              </div>
            </div>
          )
        })}
      </div>
      <div className="wf-axis">
        <span>0</span>
        <span>{seconds(total / 2)}</span>
        <span>{seconds(total)}</span>
      </div>
      <div className="legend">
        {kindsPresent.map((k) => (
          <span key={k} className="chip">
            <span className="swatch" style={{ background: KIND_COLOR[k] }} /> {k}
          </span>
        ))}
        {hasErrors && (
          <span className="chip">
            <span className="swatch" style={{ background: 'var(--status-critical)' }} />
            {'⚠'} error
          </span>
        )}
      </div>
      {selected && (
        <div className="io">
          {selected.error && (
            <>
              <h3>error</h3>
              <pre className="err-text">{selected.error}</pre>
            </>
          )}
          {selected.input != null && (
            <>
              <h3>input</h3>
              <pre>{selected.input}</pre>
            </>
          )}
          {selected.output != null && (
            <>
              <h3>output</h3>
              <pre>{selected.output}</pre>
            </>
          )}
          {selected.input == null && selected.output == null && !selected.error && (
            <div className="empty">No recorded payloads on this span.</div>
          )}
        </div>
      )}
      <Tip tip={tip} />
    </div>
  )
}
