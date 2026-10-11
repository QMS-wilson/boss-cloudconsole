import { money } from './format'

export interface EditorLine {
  key: string
  iden_guid: string
  hpbh: string
  name: string
  spec: string
  unit: string
  qtyText: string
  priceText: string
  amountText: string
  priceHint: string
}

export function makeLine(hpbh: string, name: string, unit: string, spec = ''): EditorLine {
  return {
    key: 'line-' + Date.now() + '-' + Math.floor(Math.random() * 100000),
    iden_guid: '',
    hpbh,
    name,
    spec: spec || '',
    unit: unit || '',
    qtyText: '1',
    priceText: '',
    amountText: '0.00',
    priceHint: '',
  }
}

export function calcAmount(priceText: string, qtyText: string): string {
  const price = Number(priceText)
  const qty = Number(qtyText)
  if (!priceText || !qtyText || isNaN(price) || isNaN(qty)) return '0.00'
  return money(price * qty)
}

export function totals(lines: EditorLine[]): { amount: number; qty: number } {
  let amount = 0
  let qty = 0
  for (const l of lines) {
    amount += Number(l.amountText) || 0
    qty += Number(l.qtyText) || 0
  }
  return { amount, qty }
}
