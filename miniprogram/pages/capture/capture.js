const api = require('../../utils/api')
const { idempotencyKey } = require('../../utils/ids')
const { stateLabel, eventLabel, formatClock } = require('../../utils/format')

const terminalStates = ['awaiting_user', 'completed', 'failed', 'cancelled', 'budget_exhausted']
const progressByState = {
  queued: 8,
  ingesting: 18,
  retrieving_memory: 30,
  routing_skills: 42,
  planning: 54,
  executing: 66,
  drafting: 78,
  reviewing: 90,
  awaiting_user: 100,
  completed: 100,
}

Page({
  data: {
    title: '',
    learningGoal: '',
    content: '',
    charCount: 0,
    busy: false,
    sourceSaved: null,
    run: null,
    runStateLabel: '',
    currentActivity: '等待记录',
    progress: 0,
    elapsedText: '0 秒',
    events: [],
    tasksExpanded: true,
    draft: null,
    expandedUnitId: '',
    memoryCandidates: [],
    error: '',
  },

  onLoad() {
    const activeRunId = wx.getStorageSync('memoryAgentActiveRunId')
    if (activeRunId) {
      this.setData({
        busy: true,
        run: { id: activeRunId, state: 'queued' },
        currentActivity: '正在恢复上次运行',
      })
      this.startedAt = Date.now()
      this.pollRun()
    }
  },

  onShow() {
    const tabBar = typeof this.getTabBar === 'function' && this.getTabBar()
    if (tabBar) tabBar.setData({ selected: 0 })
    if (this.data.run && !terminalStates.includes(this.data.run.state)) this.schedulePoll(0)
  },

  onHide() {
    this.stopPolling()
  },

  onUnload() {
    this.stopPolling()
  },

  onTitleInput(event) {
    this.setData({ title: event.detail.value })
  },

  onGoalInput(event) {
    this.setData({ learningGoal: event.detail.value })
  },

  onContentInput(event) {
    const content = event.detail.value
    this.setData({ content, charCount: content.length })
  },

  async submitContent() {
    const content = this.data.content.trim()
    if (!content) {
      this.setData({ error: '先写下你想记住或理解的内容。' })
      return
    }

    this.stopPolling()
    this.startedAt = Date.now()
    this.lastEventSeq = 0
    this.setData({
      busy: true,
      sourceSaved: null,
      run: null,
      draft: null,
      memoryCandidates: [],
      events: [],
      tasksExpanded: true,
      error: '',
      currentActivity: '正在安全保存原文',
      progress: 4,
      elapsedText: '0 秒',
    })

    try {
      const saveStarted = Date.now()
      const source = await api.post('/sources/resolve', {
        input: content,
        title: this.data.title.trim() || null,
        learning_goal: this.data.learningGoal.trim() || '准确整理并记住这份材料',
        content_type: 'text',
        web_access_allowed: false,
      }, { timeout: 20000 })
      this.setData({
        sourceSaved: { ...source, saveMs: Math.max(1, Date.now() - saveStarted) },
        currentActivity: source.origin_type === 'url'
          ? '网页正文已解析并保存，AI 正在后台整理'
          : '原文已保存，AI 正在后台整理',
        progress: 8,
      })

      const run = await api.post('/runs', {
        source_id: source.id,
        idempotency_key: idempotencyKey('mini-run'),
      })
      wx.setStorageSync('memoryAgentActiveRunId', run.id)
      this.setData({ run, runStateLabel: stateLabel(run.state) })
      this.schedulePoll(0)
    } catch (error) {
      this.setData({ busy: false, error: error.message })
    }
  },

  schedulePoll(delay = 1500) {
    this.stopPolling()
    this.pollTimer = setTimeout(() => this.pollRun(), delay)
  },

  stopPolling() {
    if (this.pollTimer) clearTimeout(this.pollTimer)
    this.pollTimer = null
  },

  async pollRun() {
    const runId = this.data.run && this.data.run.id
    if (!runId || this.polling) return
    this.polling = true
    try {
      const [run, newEvents] = await Promise.all([
        api.get(`/runs/${runId}`),
        api.get(`/runs/${runId}/events?after_seq=${this.lastEventSeq || 0}`),
      ])
      const allEvents = this.data.events.concat(
        newEvents.map((event) => ({
          ...event,
          label: eventLabel(event),
          clock: formatClock(event.created_at),
        })),
      )
      if (newEvents.length) this.lastEventSeq = newEvents[newEvents.length - 1].seq
      const elapsedSeconds = Math.max(0, Math.floor((Date.now() - this.startedAt) / 1000))
      const latest = allEvents[allEvents.length - 1]
      this.setData({
        run,
        runStateLabel: stateLabel(run.state),
        currentActivity: latest ? latest.label : stateLabel(run.state),
        progress: progressByState[run.state] || this.data.progress,
        elapsedText: elapsedSeconds < 60 ? `${elapsedSeconds} 秒` : `${Math.floor(elapsedSeconds / 60)} 分 ${elapsedSeconds % 60} 秒`,
        events: allEvents.slice(-50),
      })

      if (run.state === 'awaiting_user') {
        await this.loadDraft(runId)
        this.setData({ busy: false, tasksExpanded: false })
        return
      }
      if (run.state === 'failed' || run.state === 'cancelled' || run.state === 'budget_exhausted') {
        wx.removeStorageSync('memoryAgentActiveRunId')
        this.setData({ busy: false, tasksExpanded: false, error: run.error_message || stateLabel(run.state) })
        return
      }
      if (run.state === 'completed') {
        wx.removeStorageSync('memoryAgentActiveRunId')
        this.setData({ busy: false, tasksExpanded: false })
        return
      }
      this.schedulePoll(1500)
    } catch (error) {
      this.setData({ currentActivity: '连接短暂中断，正在重试', error: error.message })
      this.schedulePoll(2500)
    } finally {
      this.polling = false
    }
  },

  async loadDraft(runId) {
    const draft = await api.get(`/runs/${runId}/draft`)
    this.setData({
      draft,
      expandedUnitId: draft.units.length ? draft.units[0].id : '',
      progress: 100,
      currentActivity: `已生成 ${draft.units.length} 个知识单元，等你确认`,
    })
  },

  toggleUnit(event) {
    const id = event.currentTarget.dataset.id
    this.setData({ expandedUnitId: this.data.expandedUnitId === id ? '' : id })
  },

  toggleTaskList() {
    this.setData({ tasksExpanded: !this.data.tasksExpanded })
  },

  async confirmDraft() {
    if (!this.data.draft || this.data.busy) return
    this.setData({ busy: true, error: '' })
    try {
      const draft = await api.post(`/drafts/${this.data.draft.id}/confirm`, {})
      wx.setStorageSync('memoryAgentReviewQueueDirty', true)
      this.setData({ draft })
      await this.pollRun()
      await this.loadMemoryCandidates()
      wx.showToast({ title: '已加入复习', icon: 'success' })
    } catch (error) {
      this.setData({ busy: false, error: error.message })
    }
  },

  async loadMemoryCandidates() {
    try {
      const memoryCandidates = await api.get('/memory-candidates?status=pending')
      this.setData({ memoryCandidates })
    } catch (error) {
      this.setData({ error: error.message })
    }
  },

  async decideMemory(event) {
    const { id, decision } = event.currentTarget.dataset
    try {
      await api.post(`/memory-candidates/${id}/decision`, { decision })
      this.setData({ memoryCandidates: this.data.memoryCandidates.filter((item) => item.id !== id) })
      wx.showToast({ title: decision === 'approve' ? '记忆已批准' : '已忽略', icon: 'none' })
    } catch (error) {
      this.setData({ error: error.message })
    }
  },

  goToReview() {
    wx.switchTab({ url: '/pages/review/review' })
  },

  resetCapture() {
    this.stopPolling()
    wx.removeStorageSync('memoryAgentActiveRunId')
    this.lastEventSeq = 0
    this.setData({
      title: '',
      learningGoal: '',
      content: '',
      charCount: 0,
      busy: false,
      sourceSaved: null,
      run: null,
      draft: null,
      events: [],
      tasksExpanded: true,
      memoryCandidates: [],
      error: '',
      progress: 0,
      currentActivity: '等待记录',
    })
  },
})
