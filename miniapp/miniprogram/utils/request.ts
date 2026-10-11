import { CLOUD_BASE } from '../config'
import { loadSession, saveSession } from './session'

export interface RequestOptions {
  method?: 'GET' | 'POST'
  data?: Record<string, any>
  token?: string
  openid?: string
  store?: string
}

export interface ApiFail {
  code?: string
  message?: string
}

export function request<T = any>(path: string, opts: RequestOptions = {}): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const header: Record<string, string> = { 'Content-Type': 'application/json' }
    if (opts.token) header.Authorization = 'Bearer ' + opts.token
    if (opts.openid) header['X-Openid'] = opts.openid
    if (opts.store) header['X-Store'] = opts.store
    wx.request({
      url: CLOUD_BASE + path,
      method: opts.method || 'GET',
      data: opts.data,
      header,
      success(res) {
        const body = (res.data || {}) as any
        if (res.statusCode >= 200 && res.statusCode < 300 && body.ok !== false) {
          resolve(body as T)
          return
        }
        const err = body && body.error ? body.error : {}
        const fail: ApiFail = {
          code: err.code || 'HTTP_' + res.statusCode,
          message: err.message || (typeof body.detail === 'string' ? body.detail : '请求失败(' + res.statusCode + ')'),
        }
        reject(Object.assign(new Error(fail.message), fail))
      },
      fail() {
        reject(Object.assign(new Error('网络异常，请稍后重试'), { code: 'NETWORK' } as ApiFail))
      },
    })
  })
}

/** 走当前店铺令牌调用本地 Agent 白名单 op */
export function agentRpc<T = any>(op: string, params: Record<string, any> = {}): Promise<T> {
  const s = loadSession()
  if (!s || !s.store) {
    return Promise.reject(Object.assign(new Error('尚未选择店铺，请重新进入'), { code: 'NO_STORE' }))
  }
  const useAccess = !!s.accessToken && (!s.tokenExpiresAt || Date.now() < s.tokenExpiresAt)
  const call = (token: string, store?: string) => request<any>('/api/v1/rpc', {
    method: 'POST',
    data: { op, params, store: store || undefined },
    token,
    openid: s.openid,
    store,
  }).then((r) => r.data as T)
  const first = useAccess ? call(s.accessToken as string, s.store.name) : call(s.store.token)
  return first.catch((e: any) => {
    const code = (e && e.code) || ''
    if (!s.refreshToken || (code !== 'TOKEN_INVALID' && code !== 'UNAUTHORIZED')) throw e
    return request<any>('/api/v1/auth/refresh', {
      method: 'POST',
      data: { refreshToken: s.refreshToken },
    }).then((r) => {
      const data = (r && r.data) || {}
      s.accessToken = data.accessToken
      s.refreshToken = data.refreshToken || s.refreshToken
      s.tokenExpiresAt = Date.now() + ((data.expiresIn || 43200) * 1000)
      saveSession(s)
      return call(s.accessToken as string, s.store!.name)
    })
  })
}
