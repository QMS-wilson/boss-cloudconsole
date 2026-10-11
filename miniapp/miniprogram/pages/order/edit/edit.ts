import { agentRpc } from '../../../utils/request'
import { loadSession } from '../../../utils/session'
import { takePick } from '../../../utils/pick'
import { calcAmount, makeLine, totals, EditorLine } from '../../../utils/editor'
import { money } from '../../../utils/format'
import { BillDetail, BillLine, PriceResult } from '../../../utils/types'

Page({
  needLoad: true,

  data: {
    bill_no: '',
    customer: { code: '', name: '' },
    origCustomer: { code: '', name: '' },
    note: '',
    origNote: '',
    lines: [] as EditorLine[],
    totalText: '0.00',
    totalQtyText: '0',
    saving: false,
    pendingCustomer: false,
    pendingProduct: false,
  },

  onLoad(q: any) {
    this.setData({ bill_no: q && q.bill_no ? decodeURIComponent(q.bill_no) : '' })
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
        this.repriceAll()
      } else if (pick.kind === 'product' && this.data.pendingProduct) {
        const p = pick.value || {}
        this.addProductLine(p)
        this.setData({ pendingProduct: false })
      }
    }
    if (this.needLoad) {
      this.needLoad = false
      if (this.data.bill_no) this.loadBill()
    }
  },

  loadBill() {
    wx.showLoading({ title: '加载中…' })
    agentRpc<BillDetail>('bill.get', { bill_no: this.data.bill_no })
      .then((b) => {
        wx.hideLoading()
        if (!b.editable) {
          wx.showModal({
            title: '该单不可修改',
            content: (b.editable_reasons || []).join('；') || '只读单据',
            showCancel: false,
            success: () => wx.navigateBack(),
          })
          return
        }
        const lines: EditorLine[] = (b.lines || []).map((x: BillLine) => ({
          key: x.iden_guid || 'old-' + x.index,
          iden_guid: x.iden_guid,
          hpbh: x.hpbh,
          name: x.name,
          spec: '',
          unit: x.unit || '',
          qtyText: String(x.qty),
          priceText: String(x.price),
          amountText: money(x.amount),
          priceHint: '',
        }))
        this.setData({
          customer: { code: (b.customer && b.customer.code) || '', name: (b.customer && b.customer.name) || '' },
          origCustomer: { code: (b.customer && b.customer.code) || '', name: (b.customer && b.customer.name) || '' },
          note: b.bz_xs || '',
          origNote: b.bz_xs || '',
          lines,
        })
        this.recalc()
      })
      .catch((e: any) => {
        wx.hideLoading()
        wx.showModal({ title: '加载失败', content: (e && e.message) || '请重试', showCancel: false })
      })
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

  repriceAll() {
    const { lines, customer } = this.data
    if (!customer.code) return
    lines.forEach((line, i) => {
      if (line.iden_guid) return // 老行保留原价，由保存引擎处理
      this.resolveNewPrice(i)
    })
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
      content: '确定删除「' + line.name + '」吗？保存后该行会从单据移除。',
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
    if (this.data.saving) return
    const { customer, lines } = this.data
    if (!customer.code) {
      wx.showToast({ title: '请先选择客户', icon: 'none' })
      return
    }
    if (!lines.length) {
      wx.showToast({ title: '至少保留一行商品', icon: 'none' })
      return
    }
    for (const l of lines) {
      const qty = Number(l.qtyText)
      if (!l.qtyText || isNaN(qty) || qty <= 0) {
        wx.showToast({ title: '「' + l.name + '」数量不正确', icon: 'none' })
        return
      }
    }
    wx.showModal({
      title: '确认保存',
      content: '保存后系统按 CRM 规则重算金额并联动库存/最近销价。',
      confirmColor: '#1677ff',
      success: (r) => {
        if (r.confirm) this.save()
      },
    })
  },

  save() {
    const { bill_no, customer, note, lines } = this.data
    const header: Record<string, any> = { BZ_XS: note }
    if (customer.code !== this.data.origCustomer.code) header.ItemBh = customer.code
    const payloadLines = lines.map((l) => {
      const row: Record<string, any> = {
        hpbh: l.hpbh,
        unit: l.unit,
        qty: Number(l.qtyText),
      }
      if (l.iden_guid) row.iden_guid = l.iden_guid
      if (l.priceText) row.price = Number(l.priceText)
      return row
    })
    this.setData({ saving: true })
    wx.showLoading({ title: '保存中…', mask: true })
    agentRpc<any>('salesbill.update', { bill_no, header, lines: payloadLines })
      .then(() => {
        wx.hideLoading()
        wx.showToast({ title: '保存成功', icon: 'success' })
        setTimeout(() => wx.navigateBack(), 700)
      })
      .catch((e: any) => {
        this.setData({ saving: false })
        wx.hideLoading()
        wx.showModal({ title: '保存失败', content: (e && e.message) || '请重试', showCancel: false })
      })
  },
})
