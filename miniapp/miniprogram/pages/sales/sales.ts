import { agentRpc } from '../../utils/request'
import { loadSession } from '../../utils/session'
import { monthStartStr, todayStr, weekStartStr, yearStartStr } from '../../utils/date'
import { orderFromBill, OrderView } from '../../utils/bills'
import { BillListItem } from '../../utils/types'

Page({
  data: {
    tabs: ['全部', '今天', '本周', '本月'],
    activeTab: '全部',
    keyword: '',
    list: [] as OrderView[],
    displayList: [] as OrderView[],
    loading: false,
    searched: false,
  },

  onShow() {
    const s = loadSession()
    if (!s || !s.store) {
      wx.reLaunch({ url: '/pages/login/login' })
      return
    }
    this.syncTab('sales')
    this.load()
  },

  syncTab(selected: string) {
    const self: any = this
    const bar: any = self.getTabBar && self.getTabBar()
    if (bar) bar.setData({ selected })
  },

  range() {
    const today = todayStr()
    const tab = this.data.activeTab
    if (tab === '今天') return { from: today, to: today }
    if (tab === '本周') return { from: weekStartStr(), to: today }
    if (tab === '本月') return { from: monthStartStr(), to: today }
    return { from: yearStartStr(), to: today }
  },

  load() {
    this.setData({ loading: true })
    const { from, to } = this.range()
    agentRpc<BillListItem[]>('bill.list', { date_from: from, date_to: to, limit: 200 })
      .then((rows) => {
        const list = (rows || []).map(orderFromBill)
        this.setData({ list, searched: true, loading: false })
        this.applyFilter()
      })
      .catch((e: any) => {
        this.setData({ loading: false })
        wx.showToast({ title: (e && e.message) || '查询失败', icon: 'none' })
      })
  },

  applyFilter() {
    const kw = this.data.keyword.trim().toLowerCase()
    const displayList = kw
      ? this.data.list.filter(
          (x) =>
            x.bill_no.toLowerCase().indexOf(kw) >= 0 ||
            (x.name || '').toLowerCase().indexOf(kw) >= 0
        )
      : this.data.list
    this.setData({ displayList })
  },

  onTab(e: any) {
    const tab = e.currentTarget.dataset.tab
    if (tab === this.data.activeTab) return
    this.setData({ activeTab: tab, keyword: '' })
    this.load()
  },

  onKeyword(e: any) {
    this.setData({ keyword: e.detail.value })
    this.applyFilter()
  },

  onSearch() {
    this.applyFilter()
  },

  onClear() {
    this.setData({ keyword: '' })
    this.applyFilter()
  },

  onOpen(e: any) {
    const no = e.currentTarget.dataset.no
    if (no) wx.navigateTo({ url: '/pages/order/detail/detail?bill_no=' + encodeURIComponent(no) })
  },

  onCreate() {
    wx.navigateTo({ url: '/pages/order/create/create' })
  },
})
