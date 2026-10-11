import { queryMyStores } from '../../utils/auth'
import { loadSession, saveSession, clearSession, SessionInfo, StoreInfo } from '../../utils/session'
import { agentRpc } from '../../utils/request'
import { monthStartStr, todayStr } from '../../utils/date'
import { money } from '../../utils/format'
import { BillListItem } from '../../utils/types'
import { orderFromBill } from '../../utils/bills'

Page({
  data: {
    avatarChar: '微',
    userName: '',
    roleText: '销售经理',
    storeTitle: '',
    storeOnline: false,
    storeCount: 0,
    monthAmountText: '0.00',
    monthCount: '0',
    recentOrders: [] as any[],
  },

  onShow() {
    const s = loadSession()
    if (!s || !s.store) {
      wx.reLaunch({ url: '/pages/login/login' })
      return
    }
    this.syncTab('profile')
    this.apply(s)
    this.loadMonth()
  },

  loadMonth() {
    agentRpc<BillListItem[]>('bill.list', {
      date_from: monthStartStr(),
      date_to: todayStr(),
      limit: 500,
    })
      .then((rows) => {
        const list = rows || []
        const amount = list.reduce((a, x) => a + (Number(x.amount) || 0), 0)
        this.setData({
          monthAmountText: money(amount),
          monthCount: String(list.length),
          recentOrders: list.slice(0, 3).map(orderFromBill),
        })
      })
      .catch(() => { /* 保持占位，不打断页面 */ })
  },

  onOpen(e: any) {
    const no = e.currentTarget.dataset.no
    if (no) wx.navigateTo({ url: '/pages/order/detail/detail?bill_no=' + encodeURIComponent(no) })
  },

  syncTab(selected: string) {
    const self: any = this
    const bar: any = self.getTabBar && self.getTabBar()
    if (bar) bar.setData({ selected })
  },

  apply(s: SessionInfo) {
    const st = s.store as StoreInfo
    const name = s.name || '微信老板'
    this.setData({
      avatarChar: (name || '微').slice(0, 1),
      userName: name,
      roleText: s.stores && s.stores.length > 1 ? '多店管理员' : '销售经理',
      storeTitle: st.title,
      storeOnline: st.online,
      storeCount: (s.stores || []).length,
    })
  },

  onSwitchStore() {
    const s = loadSession()
    if (!s) return
    const titles = (s.stores || []).map((x) => x.title)
    wx.showActionSheet({
      itemList: titles,
      success: (res) => {
        const next = s.stores![res.tapIndex]
        s.store = next
        saveSession(s)
        this.apply(s)
        wx.showToast({ title: '已切换到 ' + next.title, icon: 'none' })
      },
    })
  },

  onRecheck() {
    const s = loadSession()
    if (!s) return
    wx.showLoading({ title: '检查中…' })
    queryMyStores(s.openid)
      .then((me) => {
        const cur = loadSession()
        if (!cur) return
        cur.name = me.name
        cur.stores = me.stores
        const match = me.stores.find((x) => x.name === cur.store!.name)
        if (!match || !me.authorized) {
          clearSession()
          wx.showToast({ title: '授权已失效', icon: 'none' })
          setTimeout(() => wx.reLaunch({ url: '/pages/login/login' }), 800)
          return
        }
        cur.store = match
        saveSession(cur)
        this.apply(cur)
        wx.showToast({ title: '授权正常', icon: 'success' })
      })
      .catch(() => wx.showToast({ title: '检查失败，请重试', icon: 'none' }))
      .finally(() => wx.hideLoading())
  },

  onCopyOpenid() {
    const s = loadSession()
    if (!s) return
    wx.setClipboardData({
      data: s.openid,
      success: () => wx.showToast({ title: 'openid 已复制', icon: 'none' }),
    })
  },

  onLogout() {
    wx.showModal({
      title: '退出登录',
      content: '退出后需重新进入小程序，本地店铺令牌会清除。',
      confirmColor: '#ff4d4f',
      success: (r) => {
        if (!r.confirm) return
        clearSession()
        wx.reLaunch({ url: '/pages/login/login' })
      },
    })
  },
})
