import { loadSession } from '../../../utils/session'
import { agentRpc } from '../../../utils/request'
import { takePick } from '../../../utils/pick'

Page({
  data: {
    types: [] as { code: string; name: string }[],
    typeIndex: -1,
    name: '',
    shortName: '',
    contact: '',
    phone: '',
    address: '',
    remark: '',
  },

  onShow() {
    const s = loadSession()
    if (!s || !s.store) wx.reLaunch({ url: '/pages/login/login' })
    if (!this.data.types.length) {
      agentRpc<any[]>('customer.types', {})
        .then((rows) => this.setData({ types: rows || [] }))
        .catch(() => { /* 类型加载失败可手填 */ })
    }
  },

  onName(e: any) { this.setData({ name: e.detail.value }) },
  onShort(e: any) { this.setData({ shortName: e.detail.value }) },
  onType(e: any) { this.setData({ typeIndex: Number(e.detail.value) }) },
  onContact(e: any) { this.setData({ contact: e.detail.value }) },
  onPhone(e: any) { this.setData({ phone: e.detail.value }) },
  onAddress(e: any) { this.setData({ address: e.detail.value }) },
  onRemark(e: any) { this.setData({ remark: e.detail.value }) },

  onSave() {
    const { name, shortName, typeIndex, types, contact, phone, address, remark } = this.data
    if (!name.trim()) {
      wx.showToast({ title: '请填写客户名称', icon: 'none' })
      return
    }
    const category = typeIndex >= 0 && types[typeIndex] ? types[typeIndex].code : ''
    wx.showLoading({ title: '保存中…', mask: true })
    agentRpc<any>('customer.save', {
      name,
      category,
      contact,
      phone,
      tel: '',
      address,
      remark,
    })
      .then((r) => {
        wx.hideLoading()
        wx.showToast({ title: '客户已保存', icon: 'success' })
        setTimeout(() => wx.navigateBack(), 700)
      })
      .catch((e: any) => {
        wx.hideLoading()
        wx.showModal({ title: '保存失败', content: (e && e.message) || '请重试', showCancel: false })
      })
  },
})
