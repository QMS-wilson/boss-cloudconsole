import { agentRpc } from '../../../utils/request'
import { loadSession } from '../../../utils/session'
import { money, qtyText } from '../../../utils/format'
import { dateOnly, fmtTime } from '../../../utils/date'
import { BillDetail, BillLine } from '../../../utils/types'

Page({
  data: {
    bill_no: '',
    bill: {} as any,
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
    if (this.data.bill_no) this.load()
  },

  load() {
    wx.showLoading({ title: '加载中…' })
    agentRpc<BillDetail>('bill.get', { bill_no: this.data.bill_no })
      .then((b) => {
        const lines = (b.lines || []).map((x: BillLine) => ({
          ...x,
          qtyText: qtyText(x.qty),
          priceText: money(x.price),
          amountText: money(x.amount),
        }))
        this.setData({
          bill: {
            ...b,
            lines,
            statusText: b.is_auditing ? '已审核' : '未审核',
            statusClass: b.is_auditing ? 'done' : 'waiting',
            customerName: (b.customer && b.customer.name) || '',
            ndateText: dateOnly(b.ndate),
            auditing_time: b.auditing_time ? fmtTime(b.auditing_time) : '',
            qtyText: qtyText(b.totals.qty),
            amountText: money(b.totals.amount),
            amountHsText: money(b.totals.amount_hs),
            lineCount: lines.length,
          },
        })
        wx.hideLoading()
      })
      .catch((e: any) => {
        wx.hideLoading()
        wx.showModal({
          title: '加载失败',
          content: (e && e.message) || '请重试',
          showCancel: false,
          success: () => wx.navigateBack(),
        })
      })
  },

  onEdit() {
    wx.navigateTo({ url: '/pages/order/edit/edit?bill_no=' + encodeURIComponent(this.data.bill_no) })
  },

  onShowReasons() {
    const r = (this.data.bill.editable_reasons || []) as string[]
    wx.showModal({
      title: '不可修改',
      content: r.length ? r.join('；') : '该单据为只读状态',
      showCancel: false,
    })
  },
})
