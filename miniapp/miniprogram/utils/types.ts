export interface BillListItem {
  bill_no: string
  use_no: string
  ndate: string
  customer: { code: string; name: string }
  is_auditing: boolean | number
  bill_state: number
  amount: number
  qty: number
}

export interface BillLine {
  iden_guid: string
  index: number
  hpbh: string
  name: string
  unit: string
  qty: number
  price: number
  amount: number
}

export interface BillDetail {
  bill_no: string
  use_no: string
  ndate: string
  bz_xs: string
  customer: { code: string; name: string }
  warehouse: string
  is_auditing: boolean | number
  auditing_time: string | null
  totals: { amount: number; amount_hs: number; qty: number }
  lines: BillLine[]
  editable: boolean
  editable_reasons: string[]
}

export interface CustomerItem {
  code: string
  name: string
  py: string
  category: string
  contact: string
  phone: string
  tel: string
  address: string
  remark: string
  price_type_xs: number | string
  price_trace: number | string
  due: number
  sale_sum: number
}

export interface ProductItem {
  hpbh: string
  name: string
  spec: string
  py: string
  unit: string
  sale_price: number
  stock: number
}

export interface DashboardStats {
  month_amount: number
  today_amount: number
  today_count: number
  receivable_total: number
}

export interface PriceResult {
  price: number
  source: string
  unit: string
  rate: number
  recent: { bill_no: string; sale_date: string } | null
  manual: boolean
}
