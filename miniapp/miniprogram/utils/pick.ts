export const PICK_KEY = 'v3_pick_result'

export interface PickPayload {
  kind: 'customer' | 'product'
  value: any
}

export function storePick(p: PickPayload): void {
  wx.setStorageSync(PICK_KEY, p)
}

export function takePick(): PickPayload | null {
  const raw = wx.getStorageSync(PICK_KEY)
  wx.removeStorageSync(PICK_KEY)
  return raw ? (raw as PickPayload) : null
}
