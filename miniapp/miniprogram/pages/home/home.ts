import { agentRpc } from '../../utils/request'
import { loadSession } from '../../utils/session'
import { monthStartStr, todayStr } from '../../utils/date'
import { money } from '../../utils/format'
import { orderFromBill, OrderView } from '../../utils/bills'
import { BillListItem, DashboardStats } from '../../utils/types'

const WEEKDAYS = ['日', '一', '二', '三', '四', '五', '六']

Page({
  data: {
    userName: '老板',
    dateLabel: '',
    monthAmount: '¥0.00',
    todayAmount: '¥0.00',
    todayCount: '0',
    receivableText: '¥0.00',
    recent: [] as OrderView[],
    recentLoading: true,
  },

  onShow() {
    const s = loadSession()
    if (!s || !s.store) {
      wx.reLaunch({ url: '/pages/login/login' })
      return
    }
    this.syncTab('home')
    this.refresh(s)
  },

  syncTab(selected: string) {
    const self: any = this
    const bar: any = self.getTabBar && self.getTabBar()
    if (bar) bar.setData({ selected })
  },

  async refresh(s: any) {
    const d = new Date()
    this.setData({
      userName: s.name || '老板',
      dateLabel: d.getFullYear() + '年' + (d.getMonth() + 1) + '月' + d.getDate() + '日 星期' + WEEKDAYS[d.getDay()] + ' | 祝你今天业绩满满',
    })
    const today = todayStr()
    const month = monthStartStr()
    try {
      const [monthList, todayList, dash] = await Promise.all([
        agentRpc<BillListItem[]>('bill.list', { date_from: month, date_to: today, limit: 200 }),
        agentRpc<BillListItem[]>('bill.list', { date_from: today, date_to: today, limit: 200 }),
        agentRpc<DashboardStats>('system.dashboard', { month_start: month, today }),
      ])
      const sum = (rows: BillListItem[]) => rows.reduce((a, x) => a + (Number(x.amount) || 0), 0)
      const recent = (monthList || []).slice(0, 3).map(orderFromBill)
      this.setData({
        monthAmount: '¥' + money(sum(monthList || [])),
        todayAmount: '¥' + money(sum(todayList || [])),
        todayCount: String((todayList || []).length),
        receivableText: '¥' + money((dash && dash.receivable_total) || 0),
        recent,
        recentLoading: false,
      })
    } catch (e: any) {
      this.setData({ recentLoading: false })
      wx.showToast({ title: (e && e.message) || '数据加载失败', icon: 'none' })
    }
  },

  onCreate() {
    wx.navigateTo({ url: '/pages/order/create/create' })
  },

  onOpen(e: any) {
    const no = e.currentTarget.dataset.no
    if (no) wx.navigateTo({ url: '/pages/order/detail/detail?bill_no=' + encodeURIComponent(no) })
  },

  goSales() {
    wx.switchTab({ url: '/pages/sales/sales' })
  },

  goCustomers() {
    wx.switchTab({ url: '/pages/customers/customers' })
  },

  goProducts() {
    wx.navigateTo({ url: '/pages/product/list/list' })
  },

  goReport() {
    wx.navigateTo({ url: '/pages/report/report' })
  },

  onReceivable() {
    wx.switchTab({ url: '/pages/customers/customers' })
  },
})
