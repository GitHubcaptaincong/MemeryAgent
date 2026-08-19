Component({
  data: {
    selected: 0,
    badge: 0,
    hidden: false,
    tabs: [
      { pagePath: '/pages/capture/capture', text: '整理', icon: '＋' },
      { pagePath: '/pages/review/review', text: '复习', icon: '◉' },
      { pagePath: '/pages/reminders/reminders', text: '提醒', icon: '◷' },
    ],
  },

  methods: {
    switchTab(event) {
      const index = Number(event.currentTarget.dataset.index)
      const pagePath = event.currentTarget.dataset.path
      if (index === this.data.selected) return
      wx.switchTab({ url: pagePath })
    },
  },
})
