import { useState, type ReactNode, type RefObject } from 'react'

export interface TipState {
  x: number
  y: number
  node: ReactNode
}

/** Shared hover tooltip: charts set it from mouse events, positioned
 *  relative to their own container element. */
export function useTip(containerRef: RefObject<HTMLElement | null>) {
  const [tip, setTip] = useState<TipState | null>(null)
  const show = (e: React.MouseEvent, node: ReactNode) => {
    const host = containerRef.current
    if (!host) return
    const r = host.getBoundingClientRect()
    setTip({ x: e.clientX - r.left, y: e.clientY - r.top, node })
  }
  const hide = () => setTip(null)
  return { tip, show, hide }
}

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
