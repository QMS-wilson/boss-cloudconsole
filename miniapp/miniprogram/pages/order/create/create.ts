import { agentRpc } from '../../../utils/request'
import { loadSession } from '../../../utils/session'
import { takePick } from '../../../utils/pick'
import { calcAmount, makeLine, totals, EditorLine } from '../../../utils/editor'
import { PriceResult } from '../../../utils/types'

Page({
  data: {
    customer: { code: '', name: '' },
    lines: [] as EditorLine[],
    note: '',
    totalText: '0.00',
    totalQtyText: '0',
    pendingCustomer: false,
    pendingProduct: false,
  },

  onLoad(q: any) {
    if (q && q.customer) {
      this.setData({
        customer: {
          code: decodeURIComponent(q.customer),
          name: q.name ? decodeURIComponent(q.name) : '',
        },
      })
    }
  },

  onShow() {
    const s = loadSession()
    if (!s || !s.store) {
      wx.reLaunch({ url: '/pages/login/login' })
      return
    }
    const pick = takePick()
    if (pick) {
      if (pick.kind === 'customer' && this.data.pendingCustomer) {
        const c = pick.value || {}
        this.setData({
          customer: { code: c.code || '', name: c.name || '' },
          pendingCustomer: false,
        })
      } else if (pick.kind === 'product' && this.data.pendingProduct) {
        const p = pick.value || {}
        this.addProductLine(p)
        this.setData({ pendingProduct: false })
      }
    }
  },

  onPickCustomer() {
    this.setData({ pendingCustomer: true })
    wx.navigateTo({ url: '/pages/pick/customer/customer' })
  },

  onAddProduct() {
    this.setData({ pendingProduct: true })
    wx.navigateTo({ url: '/pages/product/list/list?pick=1' })
  },

  addProductLine(p: any) {
    const line = makeLine(p.hpbh, p.name, p.unit || '', p.spec || '')
    const lines = this.data.lines.concat([line])
    this.setData({ lines })
    this.recalc()
    this.resolveNewPrice(lines.length - 1)
  },

  resolveNewPrice(idx: number) {
    const { customer, lines } = this.data
    const line = lines[idx]
    if (!line || !customer.code || !line.hpbh) return
    agentRpc<PriceResult>('price.resolve', {
      customer: customer.code,
      hpbh: line.hpbh,
      unit: line.unit,
    })
      .then((r) => {
        const cur = this.data.lines[idx]
        if (!cur || cur.key !== line.key) return
        const priceText = r.price > 0 ? String(r.price) : ''
        const hint = r.manual
          ? '无最近销价也无默认价，请手工填写单价'
          : r.source === 'recent'
            ? '已按最近销价取价'
            : '已按默认价格取价'
        this.setData({
          ['lines[' + idx + '].priceText']: priceText,
          ['lines[' + idx + '].amountText']: calcAmount(priceText, cur.qtyText),
          ['lines[' + idx + '].priceHint']: hint,
        })
        this.recalc()
      })
      .catch(() => { /* 保留空价 */ })
  },

  onQtyInput(e: any) {
    const idx = Number(e.currentTarget.dataset.index)
    const value = (e.detail.value || '').trim()
    const line = this.data.lines[idx]
    if (!line) return
    this.setData({
      ['lines[' + idx + '].qtyText']: value,
      ['lines[' + idx + '].amountText']: calcAmount(line.priceText, value),
    })
    this.recalc()
  },

  onStep(e: any) {
    const idx = Number(e.currentTarget.dataset.index)
    const delta = Number(e.currentTarget.dataset.step)
    const line = this.data.lines[idx]
    if (!line) return
    const base = Number(line.qtyText) || 0
    const next = Math.max(1, base + delta)
    this.setData({
      ['lines[' + idx + '].qtyText']: String(next),
      ['lines[' + idx + '].amountText']: calcAmount(line.priceText, String(next)),
    })
    this.recalc()
  },

  onDeleteLine(e: any) {
    const idx = Number(e.currentTarget.dataset.index)
    const line = this.data.lines[idx]
    if (!line) return
    wx.showModal({
      title: '删除明细',
      content: '确定删除「' + line.name + '」吗？',
      confirmColor: '#1677ff',
      success: (r) => {
        if (!r.confirm) return
        const lines = this.data.lines.filter((_, i) => i !== idx)
        this.setData({ lines })
        this.recalc()
      },
    })
  },

  onNote(e: any) {
    this.setData({ note: e.detail.value })
  },

  recalc() {
    const t = totals(this.data.lines)
    this.setData({
      totalText: t.amount.toFixed(2),
      totalQtyText: String(t.qty),
    })
  },

  onSave() {
    const { customer, lines, note } = this.data
    if (!customer.code) {
      wx.showToast({ title: '请先选择客户', icon: 'none' })
      return
    }
    if (!lines.length) {
      wx.showToast({ title: '请至少添加一行商品', icon: 'none' })
      return
    }
    for (const l of lines) {
      const qty = Number(l.qtyText)
      if (!l.qtyText || isNaN(qty) || qty <= 0) {
        wx.showToast({ title: '「' + l.name + '」数量不正确', icon: 'none' })
        return
      }
    }
    wx.showLoading({ title: '开单中…', mask: true })
    const payloadLines = lines.map((l) => {
      const row: Record<string, any> = {
        hpbh: l.hpbh,
        unit: l.unit,
        qty: Number(l.qtyText),
      }
      if (l.priceText) row.price = Number(l.priceText)
      return row
    })
    agentRpc<any>('salesbill.create', {
      customer: customer.code,
      lines: payloadLines,
      note,
    })
      .then((r) => {
        wx.hideLoading()
        wx.showToast({ title: '开单成功', icon: 'success' })
        setTimeout(() => {
          const no = (r && r.bill && r.bill.bill_no) || ''
          if (no) wx.redirectTo({ url: '/pages/order/detail/detail?bill_no=' + encodeURIComponent(no) })
          else wx.navigateBack()
        }, 700)
      })
      .catch((e: any) => {
        wx.hideLoading()
        wx.showModal({ title: '开单失败', content: (e && e.message) || '请重试', showCancel: false })
      })
  },
})
