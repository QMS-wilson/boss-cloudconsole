import { agentRpc } from '../../../utils/request'
import { loadSession } from '../../../utils/session'
import { storePick } from '../../../utils/pick'
import { CustomerItem } from '../../../utils/types'

Page({
  data: {
    keyword: '',
    rows: [] as CustomerItem[],
    loading: false,
    searched: false,
  },

  onShow() {
    const s = loadSession()
    if (!s || !s.store) wx.reLaunch({ url: '/pages/login/login' })
    if (!this.data.searched) this.search('')
  },

  onKeyword(e: any) {
    this.setData({ keyword: e.detail.value })
  },

  onSearch() {
    this.search(this.data.keyword.trim())
  },

  search(keyword: string) {
    this.setData({ loading: true })
    // ★ product.search / customer.search 自 2026-09-19 起返回分页对象
    //   {items,total,limit,offset,hasMore}，不再是裸数组。
    agentRpc<any>('customer.search', { keyword, limit: 120, offset: 0 })
      .then((res) => {
        const items = Array.isArray(res) ? res : (res && res.items) || []
        const list = items.map((x: CustomerItem) => ({ ...x, trace201: Number(x.price_trace) === 201 }))
        this.setData({ rows: list, loading: false, searched: true })
      })
      .catch((e: any) => {
        this.setData({ loading: false })
        wx.showToast({ title: (e && e.message) || '搜索失败', icon: 'none' })
      })
  },

  onRow(e: any) {
    const idx = Number(e.currentTarget.dataset.index)
    const c = this.data.rows[idx]
    if (!c) return
    storePick({ kind: 'customer', value: { code: c.code, name: c.name, py: c.py, category: c.category } })
    wx.navigateBack()
  },
})
