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

function standaloneUrl(value) {
  const trimmed = String(value || '').trim()
  const match = trimmed.match(/^(https?):\/\/([^/?#\s]+)([^\s]*)$/i)
  if (!match) return null
  const pathname = (match[3] || '').split(/[?#]/)[0] || '/'
  return {
    hostname: match[2],
    origin: `${match[1].toLowerCase()}://${match[2]}`,
    pathname,
  }
}

function decorateTurn(turn, expandedTurnIds = {}) {
  const content = String(turn.user_content || '')
  const url = standaloneUrl(content)
  const draft = turn.draft
    ? { ...turn.draft, previewUnits: (turn.draft.units || []).slice(0, 3) }
    : null
  return {
    ...turn,
    draft,
    isUrl: Boolean(url),
    hostname: url ? url.hostname.replace(/^www\./, '') : '',
    shortUrl: url ? `${url.origin}${url.pathname === '/' ? '' : url.pathname}`.slice(0, 96) : '',
    isLongText: !url && content.length > 420,
    charCount: content.length,
    userPreview: content.slice(0, 140),
    expanded: Boolean(expandedTurnIds[turn.id]),
  }
}

function decorateTurns(turns, expandedTurnIds = {}) {
  return (turns || []).map((turn) => decorateTurn(turn, expandedTurnIds))
}

function conversationGroups(conversations) {
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const yesterday = today - 24 * 60 * 60 * 1000
  const groups = []
  const byLabel = {}
  conversations.forEach((conversation) => {
    const timestamp = new Date(conversation.updated_at).getTime()
    const label = timestamp >= today ? '今天' : timestamp >= yesterday ? '昨天' : '更早'
    if (!byLabel[label]) {
      byLabel[label] = { label, items: [] }
      groups.push(byLabel[label])
    }
    byLabel[label].items.push(conversation)
  })
  return groups
}

function friendlyError(error, fallback = '操作失败，请重试') {
  const message = String((error && error.message) || '')
  if (/not found/i.test(message)) return '当前云端版本尚未包含对话接口，请部署最新后端后重试。'
  return message || fallback
}

Page({
  data: {
    statusBarHeight: 20,
    navBarHeight: 44,
    content: '',
    charCount: 0,
    historyOpen: false,
    conversations: [],
    conversationGroups: [],
    historyError: '',
    activeConversation: null,
    conversationTurns: [],
    expandedTurnIds: {},
    expandedResultId: '',
    composerPlaceholder: '发消息、粘贴文章或链接…',
    composerFocused: false,
    showCharCount: false,
    scrollIntoView: '',
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
    const windowInfo = typeof wx.getWindowInfo === 'function' ? wx.getWindowInfo() : wx.getSystemInfoSync()
    const statusBarHeight = windowInfo.statusBarHeight || 20
    const menuRect = typeof wx.getMenuButtonBoundingClientRect === 'function'
      ? wx.getMenuButtonBoundingClientRect()
      : null
    const navBarHeight = menuRect && menuRect.top
      ? (menuRect.top - statusBarHeight) * 2 + menuRect.height
      : 44
    this.setData({ statusBarHeight, navBarHeight })
    this.loadConversations()
    const activeConversationId = wx.getStorageSync('memoryAgentActiveConversationId')
    if (activeConversationId) this.loadConversation(activeConversationId, false)
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
    this.loadConversations()
    if (this.data.run && !terminalStates.includes(this.data.run.state)) this.schedulePoll(0)
  },

  onHide() {
    this.stopPolling()
  },

  onUnload() {
    this.stopPolling()
  },

  onContentInput(event) {
    const content = event.detail.value
    this.setData({ content, charCount: content.length, showCharCount: content.length >= 40000 })
  },

  onComposerBlur() {
    this.setData({ composerFocused: false })
  },

  chooseQuickAction(event) {
    this.setData({
      composerPlaceholder: event.currentTarget.dataset.placeholder,
      composerFocused: true,
    })
  },

  openAttachmentMenu() {
    wx.showActionSheet({
      itemList: ['添加链接', '粘贴剪贴板内容'],
      success: ({ tapIndex }) => {
        if (tapIndex === 0) this.promptForLink()
        if (tapIndex === 1) this.pasteClipboard()
      },
    })
  },

  promptForLink() {
    wx.showModal({
      title: '添加公开链接',
      editable: true,
      placeholderText: 'https://example.com/article',
      confirmText: '添加',
      success: ({ confirm, content }) => {
        if (!confirm || !content) return
        this.setData({
          content: content.trim(),
          charCount: content.trim().length,
          composerFocused: true,
          error: '',
        })
      },
    })
  },

  pasteClipboard() {
    wx.getClipboardData({
      success: ({ data }) => {
        const content = String(data || '').slice(0, 50000)
        this.setData({
          content,
          charCount: content.length,
          showCharCount: content.length >= 40000,
          composerFocused: true,
          error: '',
        })
      },
      fail: () => wx.showToast({ title: '无法读取剪贴板', icon: 'none' }),
    })
  },

  toggleTurnContent(event) {
    const id = event.currentTarget.dataset.id
    const expandedTurnIds = { ...this.data.expandedTurnIds, [id]: !this.data.expandedTurnIds[id] }
    this.setData({
      expandedTurnIds,
      conversationTurns: decorateTurns(this.data.conversationTurns, expandedTurnIds),
    })
  },

  toggleResult(event) {
    const id = event.currentTarget.dataset.id
    this.setData({ expandedResultId: this.data.expandedResultId === id ? '' : id })
  },

  dismissError() {
    this.setData({ error: '' })
  },

  retryPage() {
    this.setData({ error: '' })
    if (this.data.activeConversation) this.loadConversation(this.data.activeConversation.id, false)
    else this.loadConversations()
  },

  scrollToBottom() {
    this.setData({ scrollIntoView: '' })
    wx.nextTick(() => this.setData({ scrollIntoView: 'conversation-bottom' }))
  },

  async loadConversations() {
    try {
      const conversations = await api.get('/conversations?limit=50')
      this.setData({ conversations, conversationGroups: conversationGroups(conversations), historyError: '' })
    } catch (error) {
      this.setData({ historyError: friendlyError(error, '历史对话加载失败，请重试。') })
    }
  },

  openHistory() {
    this.setData({ historyOpen: true })
    this.loadConversations()
  },

  closeHistory() {
    this.setData({ historyOpen: false })
  },

  async selectConversation(event) {
    if (this.data.busy) return
    const id = event.currentTarget.dataset.id
    await this.loadConversation(id, true)
  },

  async loadConversation(id, closeDrawer = true) {
    this.setData({ error: '' })
    try {
      const detail = await api.get(`/conversations/${id}`)
      const turns = detail.turns || []
      const latest = turns.length ? turns[turns.length - 1] : null
      const latestRunning = Boolean(
        latest && latest.run_state && !terminalStates.includes(latest.run_state),
      )
      wx.setStorageSync('memoryAgentActiveConversationId', id)
      this.setData({
        historyOpen: closeDrawer ? false : this.data.historyOpen,
        activeConversation: detail.conversation,
        conversationTurns: decorateTurns(turns, this.data.expandedTurnIds),
        busy: latestRunning,
        draft: latest && latest.draft ? latest.draft : null,
        run: latest && latest.run_id ? { id: latest.run_id, state: latest.run_state } : null,
        runStateLabel: latest && latest.run_state ? stateLabel(latest.run_state) : '',
        events: [],
        tasksExpanded: false,
        memoryCandidates: [],
        progress: latest && terminalStates.includes(latest.run_state) ? 100 : 0,
        currentActivity: latest && latest.assistant_summary ? latest.assistant_summary : '等待继续对话',
      })
      if (latestRunning) {
        this.startedAt = new Date(latest.created_at).getTime() || Date.now()
        this.lastEventSeq = 0
        this.schedulePoll(0)
      }
      this.scrollToBottom()
    } catch (error) {
      this.setData({ error: friendlyError(error, '对话加载失败，请重试。') })
    }
  },

  newConversation() {
    if (this.data.busy) return
    this.stopPolling()
    wx.removeStorageSync('memoryAgentActiveRunId')
    wx.removeStorageSync('memoryAgentActiveConversationId')
    this.lastEventSeq = 0
    this.setData({
      historyOpen: false,
      activeConversation: null,
      conversationTurns: [],
      expandedTurnIds: {},
      expandedResultId: '',
      content: '',
      charCount: 0,
      sourceSaved: null,
      run: null,
      runStateLabel: '',
      draft: null,
      events: [],
      tasksExpanded: true,
      memoryCandidates: [],
      error: '',
      progress: 0,
      currentActivity: '等待记录',
      composerPlaceholder: '发消息、粘贴文章或链接…',
    })
  },

  async refreshActiveConversation() {
    const id = this.data.activeConversation && this.data.activeConversation.id
    if (!id) return
    const detail = await api.get(`/conversations/${id}`)
    this.setData({
      activeConversation: detail.conversation,
      conversationTurns: decorateTurns(detail.turns || [], this.data.expandedTurnIds),
    })
    await this.loadConversations()
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
      run: null,
      draft: null,
      memoryCandidates: [],
      events: [],
      tasksExpanded: true,
      error: '',
      currentActivity: '正在识别输入并创建任务',
      progress: 4,
      elapsedText: '0 秒',
    })

    try {
      let conversation = this.data.activeConversation
      if (!conversation) {
        conversation = await api.post('/conversations', {})
      }
      const started = await api.post(`/conversations/${conversation.id}/turns`, {
        input: content,
        title: null,
        content_type: 'text',
        web_access_allowed: false,
        idempotency_key: idempotencyKey('mini-turn'),
      }, { timeout: 20000 })
      wx.setStorageSync('memoryAgentActiveRunId', started.run.id)
      wx.setStorageSync('memoryAgentActiveConversationId', started.conversation.id)
      this.setData({
        activeConversation: started.conversation,
        conversationTurns: this.data.conversationTurns.concat(
          decorateTurn(started.turn, this.data.expandedTurnIds),
        ),
        content: '',
        charCount: 0,
        currentActivity: '材料已保存，AI 正在后台整理',
        progress: 8,
        run: started.run,
        runStateLabel: stateLabel(started.run.state),
      })
      await this.loadConversations()
      this.scrollToBottom()
      this.schedulePoll(0)
    } catch (error) {
      this.setData({ busy: false, error: friendlyError(error) })
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
        await this.refreshActiveConversation()
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
        await this.refreshActiveConversation()
        this.setData({ busy: false, tasksExpanded: false })
        return
      }
      this.schedulePoll(1500)
    } catch (error) {
      this.setData({ currentActivity: '连接短暂中断，正在重试' })
      wx.showToast({ title: '连接中断，正在重试', icon: 'none' })
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
    this.scrollToBottom()
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
      await this.refreshActiveConversation()
      await this.loadMemoryCandidates()
      wx.showToast({ title: '已加入复习', icon: 'success' })
    } catch (error) {
      this.setData({ busy: false, error: friendlyError(error) })
    }
  },

  async loadMemoryCandidates() {
    try {
      const memoryCandidates = await api.get('/memory-candidates?status=pending')
      this.setData({ memoryCandidates })
    } catch (error) {
      this.setData({ error: friendlyError(error) })
    }
  },

  async decideMemory(event) {
    const { id, decision } = event.currentTarget.dataset
    try {
      await api.post(`/memory-candidates/${id}/decision`, { decision })
      this.setData({ memoryCandidates: this.data.memoryCandidates.filter((item) => item.id !== id) })
      wx.showToast({ title: decision === 'approve' ? '记忆已批准' : '已忽略', icon: 'none' })
    } catch (error) {
      this.setData({ error: friendlyError(error) })
    }
  },

  goToReview() {
    wx.switchTab({ url: '/pages/review/review' })
  },

  resetCapture() {
    this.newConversation()
  },
})
