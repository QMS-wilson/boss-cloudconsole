import { agentRpc } from '../../utils/request'
import { loadSession } from '../../utils/session'
import { money, qtyText } from '../../utils/format'
import { monthStartStr, todayStr, yearStartStr, fmtDate } from '../../utils/date'

function daysAgo(n: number): string {
  const d = new Date()
  d.setDate(d.getDate() - n)
  return fmtDate(d)
}

Page({
  data: {
    periods: [
      { key: 'month', label: '本月', from: monthStartStr(), to: todayStr() },
      { key: 'week', label: '近7天', from: daysAgo(6), to: todayStr() },
      { key: 'year', label: '今年', from: yearStartStr(), to: todayStr() },
    ],
    activeIndex: 0,
    periodText: '',
    loading: true,
    summary: { salesAmountText: '0.00', salesCount: 0, averageText: '0.00' },
    trend: [] as any[],
    custRank: [] as any[],
    prodRank: [] as any[],
    receivable: { totalText: '0.00', items: [] as any[] },
  },

  onShow() {
    const s = loadSession()
    if (!s || !s.store) {
      wx.reLaunch({ url: '/pages/login/login' })
      return
    }
    this.load()
  },

  onPeriod(e: any) {
    const index = Number(e.currentTarget.dataset.index)
    if (index === this.data.activeIndex) return
    this.setData({ activeIndex: index })
    this.load()
  },

  async load() {
    const p = this.data.periods[this.data.activeIndex]
    this.setData({ loading: true, periodText: p.label })
    try {
      const [sum, trend, cust, prod, rec] = await Promise.all([
        agentRpc<any>('report.sales.summary', { date_from: p.from, date_to: p.to }),
        agentRpc<any>('report.sales.trend', { date_from: p.from, date_to: p.to, granularity: 'day' }),
        agentRpc<any[]>('report.customer.ranking', { date_from: p.from, date_to: p.to, limit: 5 }),
        agentRpc<any[]>('report.product.ranking', { date_from: p.from, date_to: p.to, limit: 5 }),
        agentRpc<any>('report.receivables.summary', { only_receivable: true, limit: 5 }),
      ])
      const items = trend.items || []
      const max = items.reduce((a: number, x: any) => Math.max(a, Number(x.amount) || 0), 0) || 1
      this.setData({
        loading: false,
        summary: {
          salesAmountText: money(sum.salesAmount),
          salesCount: sum.salesCount || 0,
          averageText: money(sum.averageBillAmount),
        },
        trend: items.map((x: any) => ({
          date: x.date,
          amountText: money(x.amount),
          barWidth: Math.max(4, Math.round((Number(x.amount) || 0) / max * 100)),
        })),
        custRank: (cust || []).map((x: any) => ({ ...x, amountText: money(x.salesAmount) })),
        prodRank: (prod || []).map((x: any) => ({ ...x, amountText: money(x.salesAmount), quantityText: qtyText(x.quantity) })),
        receivable: {
          totalText: money(rec.totalReceivable),
          items: (rec.items || []).map((x: any) => ({ ...x, receivableText: money(x.receivable) })),
        },
      })
    } catch (e: any) {
      this.setData({ loading: false })
      wx.showToast({ title: (e && e.message) || '统计失败', icon: 'none' })
    }
  },
})
