const api = require('../../utils/api')
const { openEvidence } = require('../../utils/evidence')

Page({
  data: {
    loading: true, saving: false, saved: false, dirty: false, error: '',
    unit: null, sourceId: '', question: '', answer: '',
  },
  onLoad(options) {
    this.setId = options.setId
    this.unitId = options.unitId
    this.loadUnit()
  },
  onUnload() { this.unloaded = true },
  async loadUnit() {
    if (this.data.saving || this.data.dirty) return
    this.setData({ loading: true, error: '' })
    if (!this.setId || !this.unitId) {
      this.setData({ loading: false, error: '缺少知识点信息，请返回知识集重新打开。' })
      return
    }
    try {
      const detail = await api.get(`/knowledge-sets/${encodeURIComponent(this.setId)}`)
      if (this.unloaded) return
      const unit = detail.units.find((item) => item.id === this.unitId)
      if (!unit) throw new Error('知识点已不存在，请返回知识集刷新。')
      this.original = { question: unit.question, answer: unit.answer }
      this.setData({
        unit, sourceId: detail.source.id, question: unit.question, answer: unit.answer,
        loading: false, dirty: false,
      })
    } catch (error) {
      if (!this.unloaded) this.setData({ loading: false, error: error.message })
    }
  },
  onFieldInput(event) {
    const field = event.currentTarget.dataset.field
    if (!['question', 'answer'].includes(field) || !this.data.unit || this.data.saving) return
    this.setData({ [field]: event.detail.value, saved: false, error: '' })
    const dirty = this.data.question !== this.original.question || this.data.answer !== this.original.answer
    this.setData({ dirty })
    this.updateLeaveAlert(dirty)
  },
  updateLeaveAlert(dirty) {
    if (dirty && typeof wx.enableAlertBeforeUnload === 'function') {
      wx.enableAlertBeforeUnload({ message: '问答尚未保存，离开会丢失修改。' })
    } else if (!dirty && typeof wx.disableAlertBeforeUnload === 'function') {
      wx.disableAlertBeforeUnload()
    }
  },
  viewEvidence() {
    if (this.data.unit) openEvidence(this.data.unit.evidence, this.data.sourceId)
  },
  async save() {
    if (!this.data.unit || this.data.loading || this.data.saving || !this.data.dirty) return
    const question = this.data.question.trim()
    const answer = this.data.answer.trim()
    if (!question || !answer) {
      this.setData({ error: '请同时填写复习问题和答案要点。' })
      return
    }
    if (question.length > 4000 || answer.length > 8000) {
      this.setData({ error: '问题最多 4,000 字，答案最多 8,000 字。' })
      return
    }
    this.setData({ saving: true, saved: false, error: '' })
    try {
      const detail = await api.patch(`/knowledge-units/${encodeURIComponent(this.unitId)}`, { question, answer })
      if (this.unloaded) return
      const unit = detail.units.find((item) => item.id === this.unitId)
      if (!unit) throw new Error('保存响应中缺少当前知识点，请返回知识集核对。')
      this.original = { question: unit.question, answer: unit.answer }
      this.setData({ unit, question: unit.question, answer: unit.answer, saving: false, saved: true, dirty: false })
      this.updateLeaveAlert(false)
      wx.setStorageSync('memoryAgentReviewQueueDirty', true)
      this.getOpenerEventChannel().emit('saved', { detail, unitId: this.unitId })
      wx.showToast({ title: '问答已保存', icon: 'success' })
    } catch (error) {
      if (!this.unloaded) this.setData({ saving: false, error: error.message })
    }
  },
  cancel() {
    if (this.data.saving) return
    const leave = () => {
      this.updateLeaveAlert(false)
      wx.navigateBack({ fail: () => {
        this.updateLeaveAlert(this.data.dirty)
        this.setData({ error: '暂时无法返回，请使用左上角返回按钮。' })
      } })
    }
    if (!this.data.dirty) return leave()
    wx.showModal({
      title: '放弃本次修改？', content: '已保存的问答不会改变。',
      confirmText: '放弃修改', cancelText: '继续编辑',
      success: ({ confirm }) => { if (confirm && !this.data.saving) leave() },
    })
  },
})
