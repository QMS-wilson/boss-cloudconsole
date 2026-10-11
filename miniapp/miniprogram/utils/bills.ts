import { BillListItem } from './types'
import { money, qtyText } from './format'
import { listTime } from './date'

export interface OrderView {
  bill_no: string
  name: string
  goodsText: string
  timeText: string
  amountText: string
  statusText: string
  statusClass: string
}

export function orderFromBill(b: BillListItem): OrderView {
  const audited = !!b.is_auditing
  return {
    bill_no: b.bill_no,
    name: (b.customer && b.customer.name) || '',
    goodsText: qtyText(b.qty) + ' 件商品',
    timeText: listTime(b.ndate),
    amountText: '¥' + money(b.amount),
    statusText: audited ? '已审核' : '未审核',
    statusClass: audited ? 'done' : 'waiting',
  }
}

export function moneyText(n: number | string | null | undefined): string {
  return '¥' + money(n)
}
