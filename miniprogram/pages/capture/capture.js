const api = require('../../utils/api')
const { idempotencyKey } = require('../../utils/ids')
const { stateLabel, eventLabel, formatClock } = require('../../utils/format')

const finishedStates = ['awaiting_user', 'completed', 'failed', 'cancelled', 'budget_exhausted']
const terminalStates = finishedStates.concat('retry_wait')

function finalizationStatus(events, now = Date.now()) {
  const valid = [...events].reverse().find((event) => (
    event.event_type === 'tool.completed'
    && event.payload && event.payload.tool === 'schema_validate'
    && event.payload.result_summary && event.payload.result_summary.valid === true
  ))
  const draftCreated = [...events].reverse().find((event) => event.event_type === 'draft.created')
  if (!valid || (draftCreated && draftCreated.seq > valid.seq)) {
    return { active: false, message: '', elapsedText: '' }
  }
  const began = new Date(valid.created_at).getTime()
  const elapsed = Number.isFinite(began) ? Math.max(0, Math.floor((now - began) / 1000)) : 0
  const message = elapsed < 8
    ? '正在完成整理…'
    : elapsed < 20
      ? '正在整理知识结构和引用…'
      : '内容较多，仍在处理中…'
  return { active: true, message, elapsedText: `${elapsed} 秒` }
}

function processingFacts(events) {
  const source = [...events].reverse().find((event) => event.event_type === 'source.loaded')
  const locate = [...events].reverse().find((event) => event.event_type === 'tool.completed' && event.payload && event.payload.tool === 'source_locate_quotes')
  const validate = [...events].reverse().find((event) => event.event_type === 'tool.completed' && event.payload && event.payload.tool === 'schema_validate' && event.payload.result_summary && event.payload.result_summary.valid === true)
  const facts = []
  if (source && source.payload.char_count) facts.push(`已读取 ${source.payload.char_count} 字材料`)
  if (locate) facts.push(`已找到 ${locate.payload.result_summary.resolved_count || 0} 条原文依据`)
  if (validate) facts.push(`已校验 ${validate.payload.result_summary.unit_count || 0} 个知识单元`)
  return facts
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

function sourceUrl(turn) {
  if (turn.source_type !== 'url' || !turn.source_url) return null
  return standaloneUrl(turn.source_url)
}

function decorateTurn(turn, expandedTurnIds = {}) {
  const content = String(turn.user_content || '')
  const url = sourceUrl(turn)
  const draft = turn.draft
    ? { ...turn.draft, previewUnits: (turn.draft.units || []).slice(0, 3) }
    : null
  return {
    ...turn,
    draft,
    isUrl: Boolean(url),
    sourceUrl: url ? turn.source_url : '',
    sourceTitle: url ? (turn.source_title || url.hostname.replace(/^www\./, '')) : '',
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
    headerRightSafeWidth: 48,
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
    canceling: false,
    sourceSaved: null,
    run: null,
    runStateLabel: '',
    currentActivity: '等待记录',
    elapsedText: '0 秒',
    finalizing: false,
    finalizingElapsedText: '',
    processingFacts: [],
    events: [],
    tasksExpanded: true,
    draft: null,
    expandedUnitId: '',
    memoryCandidates: [],
    error: '',
  },

  onLoad() {
    this.submitting = false
    this.submitCancelled = false
    this.submitRequestTask = null
    this.pendingTurn = null
    const windowInfo = typeof wx.getWindowInfo === 'function' ? wx.getWindowInfo() : wx.getSystemInfoSync()
    const statusBarHeight = windowInfo.statusBarHeight || 20
    const menuRect = typeof wx.getMenuButtonBoundingClientRect === 'function'
      ? wx.getMenuButtonBoundingClientRect()
      : null
    const navBarHeight = menuRect && menuRect.top
      ? (menuRect.top - statusBarHeight) * 2 + menuRect.height
      : 44
    const windowWidth = windowInfo.windowWidth || 375
    const headerRightSafeWidth = menuRect && menuRect.left
      ? Math.max(48, windowWidth - menuRect.left + 4)
      : 48
    this.setData({ statusBarHeight, navBarHeight, headerRightSafeWidth })
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
    if (tabBar) tabBar.setData({ selected: 0, hidden: Boolean(this.data.historyOpen) })
    this.loadConversations()
    if (this.data.busy && this.data.run && !terminalStates.includes(this.data.run.state)) this.schedulePoll(0)
  },

  onHide() {
    this.stopPolling()
    this.setTabBarHidden(false)
  },

  onUnload() {
    this.stopPolling()
    this.setTabBarHidden(false)
    if (this.submitRequestTask && typeof this.submitRequestTask.abort === 'function') {
      this.submitRequestTask.abort()
    }
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
    this.setTabBarHidden(true)
    this.loadConversations()
  },

  closeHistory() {
    this.setData({ historyOpen: false })
    this.setTabBarHidden(false)
  },

  setTabBarHidden(hidden) {
    const tabBar = typeof this.getTabBar === 'function' && this.getTabBar()
    if (tabBar) tabBar.setData({ hidden: Boolean(hidden) })
  },

  copySourceLink(event) {
    const url = String(event.currentTarget.dataset.url || '')
    if (!url) return
    wx.setClipboardData({
      data: url,
      fail: () => wx.showToast({ title: '复制失败，请重试', icon: 'none' }),
    })
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
        currentActivity: latest && latest.assistant_summary ? latest.assistant_summary : '等待继续对话',
      })
      if (latestRunning) {
        this.startedAt = new Date(latest.created_at).getTime() || Date.now()
        this.lastEventSeq = 0
        this.schedulePoll(0)
      }
      if (closeDrawer) this.setTabBarHidden(false)
      this.scrollToBottom()
    } catch (error) {
      this.setData({ error: friendlyError(error, '对话加载失败，请重试。') })
    }
  },

  resetConversationState() {
    this.pendingTurn = null
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
      finalizing: false,
      finalizingElapsedText: '',
      processingFacts: [],
      currentActivity: '等待记录',
      composerPlaceholder: '发消息、粘贴文章或链接…',
    })
    this.setTabBarHidden(false)
  },

  confirmStopForNewConversation() {
    return new Promise((resolve) => {
      wx.showModal({
        title: '停止当前任务？',
        content: '当前任务仍在运行。停止后将进入新对话。',
        confirmText: '停止并新建',
        cancelText: '继续等待',
        success: (result) => resolve(Boolean(result.confirm)),
        fail: () => resolve(false),
      })
    })
  },

  async newConversation() {
    if (this.data.canceling) return
    if (this.data.busy) {
      if (!await this.confirmStopForNewConversation()) return
      if (!await this.stopActiveRun({ notify: false })) return
    }
    this.resetConversationState()
  },

  async stopActiveRun(options = {}) {
    if (this.data.canceling) return false
    const notify = options.notify !== false
    this.setData({ canceling: true })
    this.stopPolling()
    this.submitCancelled = true
    if (this.submitRequestTask && typeof this.submitRequestTask.abort === 'function') {
      this.submitRequestTask.abort()
      this.submitRequestTask = null
    }
    try {
      let cancelledRun = this.data.run
      if (cancelledRun && cancelledRun.id && !finishedStates.includes(cancelledRun.state)) {
        cancelledRun = await api.post(`/runs/${cancelledRun.id}/cancel`, {})
      }
      wx.removeStorageSync('memoryAgentActiveRunId')
      this.setData({
        busy: false,
        canceling: false,
        tasksExpanded: false,
        run: cancelledRun ? { ...cancelledRun, state: 'cancelled' } : null,
        runStateLabel: '已取消',
        currentActivity: '本次请求已停止',
        error: notify ? '本次请求已停止，不会自动重试。' : '',
      })
      return true
    } catch (error) {
      this.setData({
        busy: false,
        canceling: false,
        tasksExpanded: false,
        error: `客户端已停止等待，但服务器取消失败：${friendlyError(error)}`,
      })
      return false
    }
  },

  async terminateAfterServerError(message = '') {
    await this.stopActiveRun({ notify: false })
    this.setData({
      error: message
        ? `服务器处理失败，本次请求已终止：${message}`
        : '服务器连接失败，本次请求已终止，不会自动重试。',
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
    if (this.data.busy || this.submitting) return
    const content = this.data.content.trim()
    if (!content) {
      this.setData({ error: '先写下你想记住或理解的内容。' })
      return
    }

    this.submitting = true
    this.submitCancelled = false
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
      elapsedText: '0 秒',
      finalizing: false,
      finalizingElapsedText: '',
      processingFacts: [],
    })

    try {
      let conversation = this.data.activeConversation
      if (!conversation) {
        conversation = await api.post('/conversations', {}, {
          onTask: (task) => { this.submitRequestTask = task },
        })
        this.submitRequestTask = null
      }
      if (
        !this.pendingTurn
        || this.pendingTurn.conversationId !== conversation.id
        || this.pendingTurn.input !== content
      ) {
        this.pendingTurn = {
          conversationId: conversation.id,
          input: content,
          key: idempotencyKey('mini-turn'),
        }
      }
      const started = await api.post(`/conversations/${conversation.id}/turns`, {
        input: content,
        title: null,
        content_type: 'text',
        web_access_allowed: false,
        idempotency_key: this.pendingTurn.key,
      }, {
        timeout: 20000,
        onTask: (task) => { this.submitRequestTask = task },
      })
      this.pendingTurn = null
      this.submitRequestTask = null
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
        run: started.run,
        runStateLabel: stateLabel(started.run.state),
      })
      await this.loadConversations()
      this.scrollToBottom()
      this.schedulePoll(0)
    } catch (error) {
      this.setData({
        busy: false,
        error: this.submitCancelled ? this.data.error : friendlyError(error),
      })
    } finally {
      this.submitRequestTask = null
      this.submitting = false
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
      const finalization = finalizationStatus(allEvents)
      this.setData({
        run,
        runStateLabel: stateLabel(run.state),
        currentActivity: finalization.active ? finalization.message : (latest ? latest.label : stateLabel(run.state)),
        elapsedText: elapsedSeconds < 60 ? `${elapsedSeconds} 秒` : `${Math.floor(elapsedSeconds / 60)} 分 ${elapsedSeconds % 60} 秒`,
        finalizing: finalization.active,
        finalizingElapsedText: finalization.elapsedText,
        processingFacts: processingFacts(allEvents),
        events: allEvents.slice(-50),
      })

      if (newEvents.some((event) => event.event_type === 'draft.created') && !this.data.draft) {
        await this.loadDraft(runId)
      }

      if (run.state === 'awaiting_user') {
        await this.loadDraft(runId)
        await this.refreshActiveConversation()
        this.setData({ busy: false, tasksExpanded: false })
        return
      }
      if (run.state === 'retry_wait') {
        await this.terminateAfterServerError(run.error_message || '服务器暂时不可用')
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
      await this.stopActiveRun({ notify: false })
      this.setData({
        busy: false,
        tasksExpanded: false,
        currentActivity: '服务器连接失败，已停止',
        error: `服务器连接失败，本次请求已终止，不会自动重试：${friendlyError(error)}`,
      })
    } finally {
      this.polling = false
    }
  },

  async loadDraft(runId) {
    const draft = await api.get(`/runs/${runId}/draft`)
    const conversationTurns = this.data.conversationTurns.map((turn) => (
      turn.run_id === runId
        ? decorateTurn({
            ...turn,
            assistant_summary: (draft.agent_summary && draft.agent_summary.overview) || turn.assistant_summary,
            draft,
          }, this.data.expandedTurnIds)
        : turn
    ))
    this.setData({
      draft,
      conversationTurns,
      expandedUnitId: draft.units.length ? draft.units[0].id : '',
      finalizing: false,
      finalizingElapsedText: '',
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
      wx.showToast({ title: '已加入知识库', icon: 'success' })
    } catch (error) {
      this.setData({ busy: false, error: friendlyError(error) })
    }
  },

  viewKnowledgeSet(event) {
    const id = event.currentTarget.dataset.id
    if (!id) return
    wx.setStorageSync('memoryAgentKnowledgeSetId', id)
    wx.switchTab({ url: '/pages/review/review' })
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
