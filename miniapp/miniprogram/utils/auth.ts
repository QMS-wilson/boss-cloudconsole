import { PORTAL_TOKEN } from '../config'
import { request } from './request'
import { loadSession, StoreInfo } from './session'

export interface MeResult {
  openid: string
  name: string
  authorized: boolean
  stores: StoreInfo[]
  accessToken?: string
  refreshToken?: string
  expiresIn?: number
}

/** 旧模式：按 openid 查授权店铺（兼容未升级的云端） */
function legacyMe(openid: string): Promise<MeResult> {
  return request<any>('/api/v1/me', { token: PORTAL_TOKEN, openid }).then((r) => ({
    openid: r.openid || openid,
    name: r.name || '',
    authorized: !!r.authorized,
    stores: r.stores || [],
  }))
}

function buildResult(body: any): MeResult {
  const data = (body && body.data) || {}
  const user = data.user || {}
  return {
    openid: user.openid || '',
    name: user.name || '',
    authorized: !!data.authorized,
    stores: data.stores || [],
    accessToken: data.accessToken,
    refreshToken: data.refreshToken,
    expiresIn: data.expiresIn,
  }
}

/** 统一登录：provider=wechat / openid / password */
export function unifiedLogin(payload: Record<string, any>): Promise<MeResult> {
  return request<any>('/api/v1/auth/login', {
    method: 'POST',
    data: payload,
    token: PORTAL_TOKEN,
  }).then(buildResult)
}

function wxLoginCode(): Promise<string> {
  return new Promise((resolve, reject) => {
    wx.login({
      success(res) {
        if (res && res.code) resolve(res.code)
        else reject(Object.assign(new Error('wx.login 未返回 code'), { code: 'WX_LOGIN_FAILED' }))
      },
      fail(e) {
        reject(Object.assign(new Error((e && e.errMsg) || 'wx.login 失败'), { code: 'WX_LOGIN_FAILED' }))
      },
    })
  })
}

/** 小程序静默登录：wx.login → 统一 /auth/login（provider=wechat） */
export async function silentLogin(): Promise<MeResult> {
  const code = await wxLoginCode()
  return unifiedLogin({ provider: 'wechat', code })
}

/** 开发模式：openid 换多端令牌；云端未开启时回退旧 /me */
export async function testOpenidLogin(openid: string): Promise<MeResult> {
  try {
    return await unifiedLogin({ provider: 'openid', openid })
  } catch (e: any) {
    if (e && (e.code === 'OPENID_LOGIN_DISABLED' || e.code === 'BAD_PROVIDER')) {
      return legacyMe(openid)
    }
    throw e
  }
}

/** 刷新授权/店铺状态：有 accessToken 走 /auth/me，否则走旧 /me */
export function queryMyStores(openid: string): Promise<MeResult> {
  const s = loadSession()
  if (s && s.accessToken) {
    return request<any>('/api/v1/auth/me', { token: s.accessToken }).then((r) => {
      const data = (r && r.data) || {}
      const user = data.user || {}
      return {
        openid: user.openid || openid,
        name: user.name || (s.name || ''),
        authorized: !!data.authorized,
        stores: data.stores || [],
        accessToken: s.accessToken,
        refreshToken: s.refreshToken,
      }
    })
  }
  return legacyMe(openid)
}
