function pad(n: number): string {
  return n < 10 ? '0' + n : String(n)
}

export function fmtDate(d: Date): string {
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())
}

export function todayStr(): string {
  return fmtDate(new Date())
}

export function monthStartStr(): string {
  const d = new Date()
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-01'
}

export function yearStartStr(): string {
  return new Date().getFullYear() + '-01-01'
}

export function weekStartStr(): string {
  const d = new Date()
  const day = d.getDay() || 7
  d.setDate(d.getDate() - day + 1)
  return fmtDate(d)
}

export function dateOnly(ds: string): string {
  return ds ? ds.slice(0, 10) : ''
}

export function fmtTime(ds: string): string {
  return ds ? ds.slice(0, 16) : '-'
}

/** "2026-09-08 14:30:00" → 列表用简短时间 */
export function listTime(ds: string): string {
  if (!ds) return '-'
  const d = dateOnly(ds)
  const t = ds.slice(11, 16)
  const today = todayStr()
  if (d === today) return '今天 ' + t
  const yd = new Date()
  yd.setDate(yd.getDate() - 1)
  if (d === fmtDate(yd)) return '昨天 ' + t
  return d + ' ' + t
}
