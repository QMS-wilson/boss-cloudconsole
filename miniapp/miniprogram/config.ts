// 云端 API 地址与入口令牌（发布前按实际环境调整）
export const CLOUD_BASE = 'http://149.88.75.125:8000'

// ★ 留空，且不要再填真令牌：这个值曾以明文提交进库，而它对应的入口令牌通道
//   （/api/v1/me、/api/v1/wechat/login）现在只用于那份**已退役**的原生小程序 ——
//   uniapp 版走的是每商户一个客户端令牌，不用入口令牌。
//   2026-09-26 实测：仓库里那份旧值打线上两个接口都是 401（云端现值不是它），
//   所以置空不会改变任何现网行为。真要用回这条通道，去后台「系统设置」重新发一个。
export const PORTAL_TOKEN = ''

// 开发/演示：云端未启用微信登录时可粘贴测试 openid
export const DEV_ALLOW_TEST_OPENID = true
