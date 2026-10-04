import type { ModelStats } from './api'
import { usd } from './format'

/** Cost by model: magnitude across categories, so one hue with direct
 *  value labels - color is not carrying identity here. */
export function CostBars({ stats }: { stats: ModelStats[] }) {
  const max = Math.max(1e-9, ...stats.map((s) => s.cost_usd))
  return (
    <div className="hbars">
      {stats.map((s) => (
        <div key={s.model} className="row">
          <span className="name" title={s.model}>
            {s.model}
          </span>
          <span className="track">
            <span className="bar" style={{ width: `${(s.cost_usd / max) * 100}%` }} />
            <span className="val">{usd(s.cost_usd)}</span>
          </span>
        </div>
      ))}
    </div>
  )
}
