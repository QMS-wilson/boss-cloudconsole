import { agentRpc } from '../../../utils/request'
import { loadSession } from '../../../utils/session'
import { money, qtyText } from '../../../utils/format'
import { ProductItem } from '../../../utils/types'

Page({
  data: {
    isNew: false,
    hpbh: '',
    name: '',
    spec: '',
    unit: '',
    salePriceText: '',
    costText: '',
    stockText: '--',
    saving: false,
  },

  onLoad(q: any) {
    const hpbh = q && q.hpbh ? decodeURIComponent(q.hpbh) : ''
    const isNew = hpbh === 'new'
    this.setData({ hpbh: isNew ? '' : hpbh, isNew })
    if (isNew) wx.setNavigationBarTitle({ title: '新建产品资料' })
  },

  onShow() {
    const s = loadSession()
    if (!s || !s.store) {
      wx.reLaunch({ url: '/pages/login/login' })
      return
    }
    if (!this.data.isNew && this.data.hpbh) this.load()
  },

  load() {
    wx.showLoading({ title: '加载中…' })
    agentRpc<any>('product.get', { hpbh: this.data.hpbh })
      .then((p) => {
        this.setData({
          name: p.name || '',
          spec: p.spec || '',
          unit: p.unit || '',
          salePriceText: p.sale_price > 0 ? String(p.sale_price) : '',
          costText: p.purchase_price > 0 ? String(p.purchase_price) : '',
          stockText: qtyText(p.stock) + (p.unit ? ' ' + p.unit : ''),
        })
        wx.hideLoading()
      })
      .catch((e: any) => {
        wx.hideLoading()
        wx.showToast({ title: (e && e.message) || '加载失败', icon: 'none' })
      })
  },

  onField(e: any) {
    const field = e.currentTarget.dataset.field as string
    this.setData({ [field]: e.detail.value })
  },

  onSave() {
    const { isNew, hpbh, name, spec, unit, salePriceText, costText } = this.data
    if (!name.trim()) {
      wx.showToast({ title: '请填写商品名称', icon: 'none' })
      return
    }
    if (isNew && !hpbh.trim()) {
      wx.showToast({ title: '请填写商品编号', icon: 'none' })
      return
    }
    this.setData({ saving: true })
    wx.showLoading({ title: '保存中…', mask: true })
    agentRpc<any>('product.save', {
      code: hpbh.trim(),
      name,
      spec,
      unit,
      sale_price: salePriceText,
      purchase_price: costText,
    })
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
