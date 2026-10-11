import { agentRpc } from '../../utils/request'
import { loadSession } from '../../utils/session'
import { money } from '../../utils/format'
import { CustomerItem } from '../../utils/types'

Page({
  data: {
    keyword: '',
    rows: [] as CustomerItem[],
    loading: false,
    searched: false,
  },

  onShow() {
    const s = loadSession()
    if (!s || !s.store) {
      wx.reLaunch({ url: '/pages/login/login' })
      return
    }
    this.syncTab('customers')
    if (!this.data.searched) this.search('')
  },

  syncTab(selected: string) {
    const self: any = this
    const bar: any = self.getTabBar && self.getTabBar()
    if (bar) bar.setData({ selected })
  },

  onKeyword(e: any) {
    this.setData({ keyword: e.detail.value })
  },

  onSearch() {
    this.search(this.data.keyword.trim())
  },

  search(keyword: string) {
    this.setData({ loading: true })
    // ★ 自 2026-09-19 起返回分页对象 {items,total,...}，不再是裸数组
    agentRpc<any>('customer.search', { keyword, limit: 120, offset: 0 })
      .then((res) => {
        const items = Array.isArray(res) ? res : (res && res.items) || []
        const list = items.map((x: CustomerItem) => ({
          ...x,
          saleText: money(x.sale_sum),
          dueText: money(x.due),
          due: Number(x.due) || 0,
          trace201: Number(x.price_trace) === 201,
        }))
        this.setData({ rows: list, loading: false, searched: true })
      })
      .catch((e: any) => {
        this.setData({ loading: false })
        wx.showToast({ title: (e && e.message) || '搜索失败', icon: 'none' })
      })
  },

  onOpen(e: any) {
    const code = e.currentTarget.dataset.code
    if (code) wx.navigateTo({ url: '/pages/customer/detail/detail?code=' + encodeURIComponent(code) })
  },

  onCreate() {
    wx.navigateTo({ url: '/pages/customer/create/create' })
  },
})
