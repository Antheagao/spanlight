export function usd(v: number | null | undefined): string {
  if (v == null) return '-'
  if (v === 0) return '$0'
  if (v < 0.01) return `$${v.toFixed(4)}`
  if (v < 1) return `$${v.toFixed(3)}`
  return `$${v.toFixed(2)}`
}

export function count(v: number | null | undefined): string {
  if (v == null) return '-'
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`
  if (v >= 10_000) return `${(v / 1_000).toFixed(1)}k`
  return v.toLocaleString()
}

export function seconds(v: number | null | undefined): string {
  if (v == null) return '-'
  if (v < 0.001) return '<1ms'
  if (v < 1) return `${Math.round(v * 1000)}ms`
  if (v < 60) return `${v.toFixed(2)}s`
  return `${Math.floor(v / 60)}m ${Math.round(v % 60)}s`
}

export function clock(unixSeconds: number, rangeSeconds: number): string {
  const d = new Date(unixSeconds * 1000)
  if (rangeSeconds <= 24 * 3600) {
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  }
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

export function when(unixSeconds: number): string {
  const delta = Date.now() / 1000 - unixSeconds
  if (delta < 60) return 'just now'
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`
  if (delta < 24 * 3600) return `${Math.floor(delta / 3600)}h ago`
  return new Date(unixSeconds * 1000).toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function pct(v: number): string {
  return `${(v * 100).toFixed(v >= 0.1 ? 0 : 1)}%`
}
