export function money(n: number | string | null | undefined): string {
  const v = Number(n || 0)
  const fixed = v.toFixed(2)
  const sign = v < 0 ? '-' : ''
  const abs = fixed.replace('-', '')
  const parts = abs.split('.')
  parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  return sign + parts.join('.')
}

export function qtyText(n: number | string | null | undefined): string {
  const v = Number(n || 0)
  return Number.isInteger(v) ? String(v) : String(Math.round(v * 100) / 100)
}
