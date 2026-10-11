Component({
  data: {
    selected: 'home',
  },
  methods: {
    onTap(e: any) {
      const key = e.currentTarget.dataset.url as string
      const map: Record<string, string> = {
        home: '/pages/home/home',
        sales: '/pages/sales/sales',
        customers: '/pages/customers/customers',
        profile: '/pages/profile/profile',
      }
      wx.switchTab({ url: map[key] })
    },
  },
})
