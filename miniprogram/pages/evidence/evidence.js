const api = require('../../utils/api')
const { locateEvidence } = require('../../utils/evidence')

Page({
  data: {
    evidence: [], activeIndex: 0, loading: false, error: '', source: null,
    selected: null, located: false, before: '', quote: '', after: '', scrollTarget: '',
  },
  onLoad() {
    this.sources = {}
    this.getOpenerEventChannel().on('evidence', ({ evidence }) => {
      this.setData({ evidence })
      this.showEvidence(0)
    })
  },
  onUnload() { this.requestId = (this.requestId || 0) + 1 },
  selectEvidence(event) { this.showEvidence(Number(event.currentTarget.dataset.index)) },
  retry() { this.showEvidence(this.data.activeIndex) },
  async showEvidence(index) {
    const selected = this.data.evidence[index]
    if (!selected) return
    const requestId = this.requestId = (this.requestId || 0) + 1
    this.setData({
      activeIndex: index, selected, loading: true, error: '', source: null,
      located: false, before: '', quote: '', after: '', scrollTarget: '',
    })
    if (!selected.source_id) {
      this.setData({ loading: false, error: '这条依据未保存可定位的原文，可阅读引文或复制来源链接。' })
      return
    }
    try {
      const source = this.sources[selected.source_id] || await api.get(`/sources/${selected.source_id}`)
      this.sources[selected.source_id] = source
      if (requestId !== this.requestId) return
      this.setData({ source, ...locateEvidence(source.raw_content, selected), loading: false })
      wx.nextTick(() => {
        if (requestId === this.requestId && this.data.located) this.setData({ scrollTarget: 'evidence-highlight' })
      })
    } catch (error) {
      if (requestId === this.requestId) this.setData({ loading: false, error: `原文加载失败：${error.message}` })
    }
  },
  copyLink() {
    const url = (this.data.selected && this.data.selected.url) || (this.data.source && this.data.source.origin_url)
    if (url) wx.setClipboardData({ data: url })
  },
})
