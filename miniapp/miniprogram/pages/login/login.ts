import { silentLogin, testOpenidLogin } from '../../utils/auth'
import { loadSession, saveSession, StoreInfo } from '../../utils/session'
import { DEV_ALLOW_TEST_OPENID } from '../../config'

interface LoginResult {
  openid: string
  name: string
  stores: StoreInfo[]
}

Page({
  data: {
    state: 'loading' as 'loading' | 'error' | 'unbound' | 'choose',
    message: '',
    openid: '',
    stores: [] as StoreInfo[],
    testOpenid: DEV_ALLOW_TEST_OPENID ? 'dev-openid-demo-001' : '',
    showTestOpenid: false,
  },

  onShow() {
    const s = loadSession()
    if (s && s.store && s.stores.length) {
      wx.switchTab({ url: '/pages/home/home' })
      return
    }
    this.startLogin()
  },

  startLogin() {
    this.setData({ state: 'loading', message: '' })
    silentLogin()
      .then((r) => this.afterLogin(r))
      .catch((e: any) => {
        if (e && e.code === 'WECHAT_NOT_CONFIGURED' && DEV_ALLOW_TEST_OPENID) {
          this.setData({
            state: 'error',
            message: '云端尚未启用微信登录，可用测试 openid 进入（仅开发调试）。',
            showTestOpenid: true,
          })
        } else {
          this.setData({
            state: 'error',
            message: (e && e.message) || '登录失败，请稍后重试',
            showTestOpenid: false,
          })
        }
      })
  },

  afterLogin(r: LoginResult) {
    const cur = loadSession() || ({} as any)
    cur.openid = r.openid
    cur.name = r.name
    cur.stores = r.stores
    if ((r as any).accessToken) {
      cur.accessToken = (r as any).accessToken
      cur.refreshToken = (r as any).refreshToken
      cur.tokenExpiresAt = Date.now() + (((r as any).expiresIn || 43200) * 1000)
    }
    saveSession(cur)
    if (!r.stores.length) {
      this.setData({ state: 'unbound', openid: r.openid, stores: [] })
      return
    }
    if (r.stores.length === 1) {
      this.enterStore(r.stores[0])
      return
    }
    this.setData({ state: 'choose', openid: r.openid, stores: r.stores })
  },

  enterStore(store: StoreInfo) {
    const s = loadSession()
    if (!s) return
    s.store = store
    saveSession(s)
    wx.switchTab({ url: '/pages/home/home' })
  },

  onChooseStore(e: any) {
    const idx = Number(e.currentTarget.dataset.index)
    const s = loadSession()
    if (s && s.stores[idx]) this.enterStore(s.stores[idx])
  },

  onCopy() {
    if (!this.data.openid) return
    wx.setClipboardData({
      data: this.data.openid,
      success: () => wx.showToast({ title: 'openid 已复制', icon: 'none' }),
    })
  },

  onRecheck() {
    this.startLogin()
  },

  onRetry() {
    this.startLogin()
  },

  onTestInput(e: any) {
    this.setData({ testOpenid: (e.detail.value || '').trim() })
  },

  onGoTest() {
    const openid = this.data.testOpenid.trim()
    if (!openid) return
    this.setData({ state: 'loading', message: '' })
    testOpenidLogin(openid)
      .then((r) => this.afterLogin(r))
      .catch((e: any) => {
        this.setData({
          state: 'error',
          message: (e && e.message) || '查询失败',
          showTestOpenid: true,
        })
      })
  },
})
