export interface StoreInfo {
  name: string
  title: string
  agent: string
  access: string
  token: string
  online: boolean
}

export interface SessionInfo {
  openid: string
  name: string
  stores: StoreInfo[]
  store?: StoreInfo
  accessToken?: string
  refreshToken?: string
  tokenExpiresAt?: number
}

const KEY = 'v3_session_v3'

export function loadSession(): SessionInfo | null {
  try {
    const raw = wx.getStorageSync(KEY)
    if (!raw) return null
    const s = typeof raw === 'string' ? JSON.parse(raw) : raw
    return s && s.openid ? (s as SessionInfo) : null
  } catch (e) {
    return null
  }
}

export function saveSession(s: SessionInfo): void {
  wx.setStorageSync(KEY, s)
}

export function clearSession(): void {
  wx.removeStorageSync(KEY)
}
