import { agentRpc } from '../../../utils/request'
import { loadSession } from '../../../utils/session'
import { yearStartStr, todayStr } from '../../../utils/date'
import { money } from '../../../utils/format'
import { orderFromBill, OrderView } from '../../../utils/bills'
import { BillListItem, CustomerItem } from '../../../utils/types'

Page({
  data: {
    code: '',
    name: '',
    category: '',
    isRecentPrice: false,
    saleText: '0.00',
    dueText: '0.00',
    due: 0,
    contact: '',
    phone: '',
    tel: '',
    address: '',
    remark: '',
    history: [] as OrderView[],
    historyLoading: true,
  },

  onLoad(q: any) {
    this.setData({ code: q && q.code ? decodeURIComponent(q.code) : '' })
  },

  onShow() {
    const s = loadSession()
    if (!s || !s.store) {
      wx.reLaunch({ url: '/pages/login/login' })
      return
    }
    if (this.data.code) this.load()
  },

  load() {
    const code = this.data.code
    const today = todayStr()
    const year = yearStartStr()
    Promise.all([
      agentRpc<CustomerItem>('customer.get', { code }),
      agentRpc<BillListItem[]>('bill.list', { date_from: year, date_to: today, customer: code, limit: 100 }),
    ])
      .then(([c, bills]) => {
        const rows = (bills || []).map(orderFromBill)
        this.setData({
          name: (c && c.name) || '',
          category: (c && c.category) || '',
          contact: (c && c.contact) || '',
          phone: (c && c.phone) || '',
          tel: (c && c.tel) || '',
          address: (c && c.address) || '',
          remark: (c && c.remark) || '',
          isRecentPrice: !!(c && c.price_trace === 201),
          saleText: money((c && c.sale_sum) || 0),
          dueText: money((c && c.due) || 0),
          due: Number((c && c.due) || 0),
          history: rows,
          historyLoading: false,
        })
      })
      .catch((e: any) => {
        this.setData({ historyLoading: false })
        wx.showToast({ title: (e && e.message) || '加载失败', icon: 'none' })
      })
  },

  onOpen(e: any) {
    const no = e.currentTarget.dataset.no
    if (no) wx.navigateTo({ url: '/pages/order/detail/detail?bill_no=' + encodeURIComponent(no) })
  },

  onNewOrder() {
    const { code, name } = this.data
    wx.navigateTo({
      url: '/pages/order/create/create?customer=' + encodeURIComponent(code) + '&name=' + encodeURIComponent(name),
    })
  },
})
