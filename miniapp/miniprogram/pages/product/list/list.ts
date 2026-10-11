import { agentRpc } from '../../../utils/request'
import { loadSession } from '../../../utils/session'
import { storePick } from '../../../utils/pick'
import { money, qtyText } from '../../../utils/format'
import { ProductItem } from '../../../utils/types'

interface Row extends ProductItem {
  stockText: string
  stock: number
}

Page({
  data: {
    keyword: '',
    rows: [] as Row[],
    loading: true,
    searched: false,
    pickMode: false,
  },

  onLoad(q: any) {
    const pick = q && String(q.pick) === '1'
    this.setData({ pickMode: pick })
    if (pick) wx.setNavigationBarTitle({ title: '选择产品' })
  },

  onShow() {
    const s = loadSession()
    if (!s || !s.store) {
      wx.reLaunch({ url: '/pages/login/login' })
      return
    }
    if (!this.data.searched) this.searchProducts('')
  },

  searchProducts(keyword: string) {
    this.setData({ loading: true })
    // ★ 自 2026-09-19 起返回分页对象 {items,total,...}，不再是裸数组
    agentRpc<any>('product.search', { keyword, limit: 120, offset: 0 })
      .then((res) => {
        const items = Array.isArray(res) ? res : (res && res.items) || []
        const list = items.map((r: ProductItem) => ({
          ...r,
          priceText: r.sale_price > 0 ? '¥' + money(r.sale_price) : '--',
          stockText: '库存 ' + qtyText(r.stock),
        }))
        this.setData({ rows: list, loading: false, searched: true })
      })
      .catch((e: any) => {
        this.setData({ loading: false })
        wx.showToast({ title: (e && e.message) || '加载失败', icon: 'none' })
      })
  },

  onKeyword(e: any) {
    this.setData({ keyword: e.detail.value })
  },

  onSearch() {
    const keyword = this.data.keyword.trim()
    this.searchProducts(keyword)
  },

  onRow(e: any) {
    const idx = Number(e.currentTarget.dataset.index)
    const p = this.data.rows[idx]
    if (!p) return
    if (!this.data.pickMode) {
      wx.navigateTo({ url: '/pages/product/detail/detail?hpbh=' + encodeURIComponent(p.hpbh) })
      return
    }
    this.pick(p)
  },

  pick(p: ProductItem) {
    storePick({ kind: 'product', value: { hpbh: p.hpbh, name: p.name, unit: p.unit || '', spec: p.spec || '' } })
    wx.navigateBack()
  },

  onCreate() {
    wx.navigateTo({ url: '/pages/product/detail/detail?hpbh=new' })
  },
})
