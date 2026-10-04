import type { TipState } from './useTip'

export function Tip({ tip }: { tip: TipState | null }) {
  if (!tip) return null
  return (
    <div
      className="tip"
      style={{ left: tip.x, top: tip.y, transform: 'translate(-50%, calc(-100% - 10px))' }}
    >
      {tip.node}
    </div>
  )
}
