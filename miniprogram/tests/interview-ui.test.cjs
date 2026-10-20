const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { createRequire } = require('node:module')
const { locateEvidence } = require('../utils/evidence')
const { processHistory } = require('../utils/run-history')

function deferred() {
  let resolve, reject
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail })
  return { promise, resolve, reject }
}

function page(name, api = {}) {
  const filename = path.resolve(__dirname, `../pages/${name}/${name}.js`)
  const realRequire = createRequire(filename)
  let instance
  const timers = new Map()
  let timerId = 0
  const storage = new Map()
  const navigations = []
  const wx = {
    getStorageSync: (key) => storage.get(key),
    setStorageSync: (key, value) => storage.set(key, value),
    removeStorageSync: (key) => storage.delete(key),
    nextTick: (fn) => fn(),
    showToast() {},
    switchTab: (options) => navigations.push(options.url),
    navigateTo: (options) => navigations.push(options.url),
  }
  vm.runInNewContext(fs.readFileSync(filename, 'utf8'), {
    require: (id) => id === '../../utils/api' ? api : realRequire(id),
    Page: (definition) => {
      instance = definition
      instance.data = structuredClone(definition.data)
      instance.setData = (values) => {
        for (const [key, value] of Object.entries(values)) {
          const parts = key.split('.')
          let target = instance.data
          for (const part of parts.slice(0, -1)) target = target[part]
          target[parts.at(-1)] = value
        }
      }
    },
    wx,
    setTimeout: (fn) => { const id = ++timerId; timers.set(id, fn); return id },
    clearTimeout: (id) => timers.delete(id),
  }, { filename })
  instance.data.loading = false
  return { instance, timers, storage, navigations, wx }
}

const event = (dataset) => ({ currentTarget: { dataset } })
const a = { id: 'a', title: '题 A', rating_options: [{ rating: 3, interval_days: 3 }] }
const b = { id: 'b', title: '题 B' }
function review(api) {
  const context = page('review', api)
  context.instance.setData({ queue: [a, b], allQueue: [a, b], activeCard: a })
  return context
}
const answered = (id, status = 'disabled') => ({
  card_id: id, attempt_id: `attempt-${id}`, answer: '我的回答', evaluation_status: status,
})

test('evidence offsets use Unicode code points, including emoji before and inside quotes', () => {
  assert.deepEqual(locateEvidence('前😀文\n引😀文\n末尾', {
    start_char: 4, end_char: 7, quote: '引😀文',
  }), { located: true, before: '前😀文\n', quote: '引😀文', after: '\n末尾' })
})

test('mismatched, absent, and out-of-bounds evidence never highlights a guessed occurrence', () => {
  for (const evidence of [
    { start_char: 0, end_char: 2, quote: '乙' },
    { start_char: null, end_char: null, quote: '甲' },
    { start_char: -1, end_char: 1, quote: '甲' },
    { start_char: 0, end_char: 99, quote: '甲' },
  ]) assert.equal(locateEvidence('甲甲', evidence).located, false)
  assert.equal(locateEvidence('甲甲', { start_char: 1, end_char: 2, quote: '甲' }).before, '甲')
})

test('switching evidence ignores an older source response', async () => {
  const first = deferred()
  const { instance: p } = page('evidence', {
    get: (url) => url.endsWith('/a') ? first.promise : Promise.resolve({ raw_content: '乙', title: '乙' }),
  })
  p.sources = {}
  p.data.evidence = [
    { source_id: 'a', start_char: 0, end_char: 1, quote: '甲' },
    { source_id: 'b', start_char: 0, end_char: 1, quote: '乙' },
  ]
  const pending = p.showEvidence(0)
  await p.showEvidence(1)
  first.resolve({ raw_content: '甲', title: '甲' })
  await pending
  assert.equal(p.data.quote, '乙')
  assert.equal(p.data.scrollTarget, 'evidence-highlight')
})

test('failed process does not invent completed stages; latest validation failure stays visible', () => {
  const history = processHistory({ state: 'failed' }, [
    { event_type: 'source.loaded', payload: { char_count: 100 } },
    { event_type: 'tool.completed', payload: { tool: 'schema_validate', result_summary: { valid: true } } },
    { event_type: 'tool.completed', payload: { tool: 'schema_validate', result_summary: { valid: false, error_count: 2 } } },
  ])
  assert.equal(history.stages[0].status, '已记录')
  assert.equal(history.stages[1].status, '暂无生成记录')
  assert.equal(history.stages[2].status, '未通过校验')
  assert.equal(history.stages[3].status, '尚未到达')
  assert.match(history.events[2].details[1], /校验问题 2 个/)
})

test('historical process reload requests all persisted events from seq zero', async () => {
  const urls = []
  const { instance: p } = page('process', { get: async (url) => {
    urls.push(url)
    return url.includes('/events') ? [{ id: 'e', seq: 1, event_type: 'draft.created' }] : { state: 'completed' }
  } })
  p.runId = 'old-run'
  await p.reload()
  assert.deepEqual(urls.sort(), ['/runs/old-run', '/runs/old-run/events?after_seq=0'])
  assert.equal(p.data.stages[3].status, '已确认')
  assert.equal(p.data.events.length, 1)
})

test('result units expand independently and historical draft confirms by its own ID', async () => {
  const urls = []
  const { instance: p } = page('capture', { post: async (url) => {
    urls.push(url)
    return { id: 'old', status: 'confirmed', units: [] }
  } })
  p.data.conversationTurns = [{ id: 't1', draft: { id: 'old', status: 'draft', units: [] } }]
  p.data.draft = { id: 'latest' }
  p.refreshActiveConversation = async () => {}
  p.loadMemoryCandidates = async () => {}
  p.toggleUnit(event({ id: 'u1' }))
  p.toggleUnit(event({ id: 'u2' }))
  assert.equal(p.data.expandedUnitIds.u1, true)
  assert.equal(p.data.expandedUnitIds.u2, true)
  await p.confirmDraft(event({ id: 'old' }))
  assert.deepEqual(urls, ['/drafts/old/confirm'])
  assert.equal(p.data.conversationTurns[0].draft.status, 'confirmed')
  assert.equal(p.data.draft.id, 'latest')
})

test('switching cards preserves per-card text, attempt result and retry key', () => {
  const { instance: p } = review()
  p.data.answer = 'A 的草稿'
  p.data.answerResult = answered('a')
  p._answerIdempotencyKey = 'retry-a'
  p.selectCard(event({ index: 1 }))
  p.onAnswerInput({ detail: { value: 'B 的草稿' } })
  p.selectCard(event({ index: 0 }))
  assert.equal(p.data.answer, 'A 的草稿')
  assert.equal(p.data.answerResult.card_id, 'a')
  assert.equal(p._answerIdempotencyKey, 'retry-a')
  p.selectCard(event({ index: 1 }))
  assert.equal(p.data.answer, 'B 的草稿')
  assert.equal(p.data.answerResult, null)
})

test('slow answer locks card/filter/section changes and duplicate submissions', async () => {
  const pending = deferred()
  let calls = 0
  const { instance: p } = review({ post: () => { calls++; return pending.promise } })
  p.data.answer = '回答 A'
  const submitting = p.submitAnswer()
  p.selectCard(event({ index: 1 }))
  p.showAllDueCards()
  await p.showKnowledgeSets()
  await p.loadReviewData()
  await p.submitAnswer()
  assert.equal(p.data.activeCard.id, 'a')
  assert.equal(p.data.section, 'practice')
  assert.equal(calls, 1)
  pending.resolve(answered('a'))
  await submitting
  assert.equal(p.data.answerResult.card_id, 'a')
  assert.equal(p.data.busy, false)
})

test('an old answer response cannot overwrite another card even if state changes externally', async () => {
  const pending = deferred()
  const { instance: p } = review({ post: () => pending.promise })
  p.data.answer = '回答 A'
  const submitting = p.submitAnswer()
  p.setData({ activeCard: b, answerResult: null, busy: false })
  pending.resolve(answered('a'))
  await submitting
  assert.equal(p.data.activeCard.id, 'b')
  assert.equal(p.data.answerResult, null)
})

test('retry after answer network failure reuses idempotency key', async () => {
  const keys = []
  const { instance: p } = review({ post: async (_url, payload) => {
    keys.push(payload.idempotency_key)
    if (keys.length === 1) throw new Error('timeout')
    return answered('a')
  } })
  p.data.answer = '回答 A'
  await p.submitAnswer()
  p.selectCard(event({ index: 1 }))
  p.selectCard(event({ index: 0 }))
  await p.submitAnswer()
  assert.equal(keys[0], keys[1])
  assert.equal(p.data.answerResult.card_id, 'a')
})

test('queue refresh after returning from evidence retains submitted answer', async () => {
  const { instance: p } = review({ get: async (url) => {
    if (url.includes('/queue')) return [a, b]
    if (url.includes('/overview')) return { due_count: 2 }
    if (url.includes('/history')) return []
    return { planned_cards: [{ id: 'a' }, { id: 'b' }] }
  } })
  p.data.answer = '已提交的回答'
  p.data.answerResult = answered('a')
  await p.loadReviewData({ silent: true })
  assert.equal(p.data.answer, '已提交的回答')
  assert.equal(p.data.answerResult.attempt_id, 'attempt-a')
  assert.equal(p.data.ratings[2].hint, '3 天后')
})

test('AI failure for a previous card does not change the new card or schedule polling', async () => {
  const pending = deferred()
  const { instance: p, timers } = review({ get: () => pending.promise })
  p.data.answerResult = answered('a', 'pending')
  p.startEvaluationPolling(p.data.answerResult)
  const timer = [...timers.values()][0]
  timers.clear()
  const polling = timer()
  p.selectCard(event({ index: 1 }))
  pending.reject(new Error('timeout'))
  await polling
  assert.equal(p.data.evaluationWaitingLong, false)
  assert.equal(p.data.answerResult, null)
  assert.equal(timers.size, 0)
})

test('typing during silent queue refresh keeps the newest text', async () => {
  const pending = deferred()
  const { instance: p } = review({ get: async (url) => {
    if (url.includes('/queue')) return pending.promise
    if (url.includes('/overview')) return { due_count: 2 }
    if (url.includes('/history')) return []
    return { planned_cards: [{ id: 'a' }, { id: 'b' }] }
  } })
  p.data.answer = '旧草稿'
  const refreshing = p.loadReviewData({ silent: true })
  p.onAnswerInput({ detail: { value: '刷新过程中继续输入的新内容' } })
  pending.resolve([a, b])
  await refreshing
  assert.equal(p.data.answer, '刷新过程中继续输入的新内容')
  assert.equal(p.data.syncing, false)
})

test('pending AI does not block user rating; next card keeps its saved draft', async () => {
  let payload
  const { instance: p } = review({ post: async (_url, value) => {
    payload = value
    return { next_due_at: '2026-10-23T00:00:00Z', interval_days: 3 }
  } })
  p.selectCard(event({ index: 1 }))
  p.data.answer = 'B 未完成的草稿'
  p.selectCard(event({ index: 0 }))
  p.data.answerResult = answered('a', 'pending')
  await p.rateAnswer(event({ rating: 3 }))
  assert.equal(payload.rating, 3)
  assert.equal(p.data.sessionCount, 1)
  assert.equal(p.data.activeCard.id, 'b')
  assert.equal(p.data.answer, 'B 未完成的草稿')
  assert.equal(p.data.answerResult, null)
  assert.equal(p.data.dailyPlan.progress_percent, 100)
})

test('unconfirmed rating retry cannot silently change user score after switching cards', async () => {
  const values = []
  const { instance: p } = review({ post: async (_url, value) => {
    values.push(value)
    if (values.length === 1) throw new Error('timeout')
    return { next_due_at: '2026-10-23T00:00:00Z' }
  } })
  p.data.answerResult = answered('a')
  await p.rateAnswer(event({ rating: 2 }))
  p.selectCard(event({ index: 1 }))
  p.selectCard(event({ index: 0 }))
  await p.rateAnswer(event({ rating: 4 }))
  assert.equal(values.length, 1)
  await p.rateAnswer(event({ rating: 2 }))
  assert.equal(values[0].idempotency_key, values[1].idempotency_key)
})

test('immediate review opens a queue filtered to the confirmed set', async () => {
  const { instance: capture, storage } = page('capture')
  capture.startReview(event({ id: 'set-1' }))
  assert.equal(storage.get('memoryAgentPracticeSetId'), 'set-1')
  const context = review()
  context.storage.set('memoryAgentPracticeSetId', 'set-1')
  let loads = 0
  context.instance.loadReviewData = () => { loads++ }
  context.instance.onShow()
  assert.equal(context.instance.data.activeKnowledgeSetId, 'set-1')
  assert.equal(context.instance.data.section, 'practice')
  assert.equal(loads, 1)
})

test('example fills the composer without a model request; replacement can be declined', () => {
  const { instance: p, wx } = page('capture', { post: () => assert.fail('must not submit automatically') })
  p.useExample()
  assert.match(p.data.content, /幂等性/)
  assert.equal(p.data.charCount, p.data.content.length)
  assert.match(p.data.inputNotice, /点击发送/)
  p.onContentInput({ detail: { value: '我自己的材料' } })
  wx.showModal = ({ success }) => success({ confirm: false })
  p.useExample()
  assert.equal(p.data.content, '我自己的材料')
})

test('material replacement cannot overwrite text typed while confirmation was open', () => {
  const { instance: p, wx } = page('capture')
  p.data.content = '旧内容'
  let confirm
  wx.showModal = ({ success }) => { confirm = success }
  p.useExample()
  p.onContentInput({ detail: { value: '较新的内容' } })
  confirm({ confirm: true })
  assert.equal(p.data.content, '较新的内容')
})

test('clipboard is inserted only on user action and oversized material is not silently truncated', () => {
  const { instance: p, wx } = page('capture')
  wx.getClipboardData = ({ success }) => success({ data: '从剪贴板读取的材料' })
  p.pasteClipboard()
  assert.equal(p.data.content, '从剪贴板读取的材料')
  p.fillMaterial('字'.repeat(50001))
  assert.equal(p.data.content, '从剪贴板读取的材料')
  assert.match(p.data.error, /50,000/)
})

test('link entry rejects non-HTTP input and fills a valid URL without submitting', () => {
  const { instance: p, wx } = page('capture', { post: () => assert.fail('must not submit') })
  wx.showModal = ({ success }) => success({ confirm: true, content: 'javascript:alert(1)' })
  p.promptForLink()
  assert.equal(p.data.content, '')
  assert.match(p.data.error, /https/)
  wx.showModal = ({ success }) => success({ confirm: true, content: 'https://example.com/article' })
  p.promptForLink()
  assert.equal(p.data.content, 'https://example.com/article')
  assert.equal(p.data.error, '')
})

const unit = { id: 'unit-a', title: '知识点 A', question: '原问题', answer: '原答案', evidence: [{ quote: '原文依据' }] }
const setDetail = () => ({ id: 'set-a', units: [structuredClone(unit)], source: { id: 'source-a' } })
async function editor(api = {}) {
  const context = page('knowledge-edit', { get: async () => setDetail(), ...api })
  const p = context.instance
  p.setId = 'set-a'
  p.unitId = 'unit-a'
  context.savedEvents = []
  p.getOpenerEventChannel = () => ({ emit: (name, payload) => context.savedEvents.push({ name, payload }) })
  await p.loadUnit()
  return context
}
const inputField = (field, value) => ({ ...event({ field }), detail: { value } })

test('editor loads both fields and evidence, and validates before sending', async () => {
  const { instance: p } = await editor({ patch: () => assert.fail('must validate first') })
  assert.equal(p.data.question, unit.question)
  assert.equal(p.data.answer, unit.answer)
  assert.equal(p.data.unit.evidence[0].quote, '原文依据')
  p.onFieldInput(inputField('question', '   '))
  await p.save()
  assert.match(p.data.error, /同时填写/)
  p.onFieldInput(inputField('question', '问'.repeat(4001)))
  await p.save()
  assert.match(p.data.error, /4,000/)
})

test('editor sends both fields together, blocks duplicate saves and reports the saved detail', async () => {
  const pending = deferred()
  const calls = []
  const context = await editor({ patch: (url, payload) => {
    calls.push({ url, payload })
    return pending.promise
  } })
  const p = context.instance
  p.onFieldInput(inputField('question', ' 新问题 '))
  p.onFieldInput(inputField('answer', ' 要点一\n要点二 '))
  const saving = p.save()
  p.onFieldInput(inputField('answer', '保存期间不能改写'))
  await p.save()
  assert.equal(calls.length, 1)
  assert.equal(calls[0].url, '/knowledge-units/unit-a')
  assert.equal(calls[0].payload.question, '新问题')
  assert.equal(calls[0].payload.answer, '要点一\n要点二')
  const detail = setDetail()
  Object.assign(detail.units[0], calls[0].payload)
  pending.resolve(detail)
  await saving
  assert.equal(p.data.dirty, false)
  assert.equal(p.data.saved, true)
  assert.equal(context.savedEvents[0].payload.unitId, 'unit-a')
  assert.equal(context.storage.get('memoryAgentReviewQueueDirty'), true)
  assert.equal(p.data.unit.evidence[0].quote, '原文依据')
})

test('failed editing keeps both inputs for retry', async () => {
  const { instance: p } = await editor({ patch: async () => { throw new Error('网络暂不可用') } })
  p.onFieldInput(inputField('question', '修改后的问题'))
  p.onFieldInput(inputField('answer', '修改后的答案'))
  await p.save()
  assert.equal(p.data.question, '修改后的问题')
  assert.equal(p.data.answer, '修改后的答案')
  assert.equal(p.data.dirty, true)
  assert.equal(p.data.saving, false)
  assert.match(p.data.error, /网络/)
})

test('cancel editing asks before discarding and never writes data', async () => {
  const { instance: p, wx } = await editor({ patch: () => assert.fail('cancel must not write') })
  let returns = 0
  wx.navigateBack = () => { returns++ }
  p.onFieldInput(inputField('answer', '未保存答案'))
  wx.showModal = ({ success }) => success({ confirm: false })
  p.cancel()
  assert.equal(returns, 0)
  assert.equal(p.data.answer, '未保存答案')
  wx.showModal = ({ success }) => success({ confirm: true })
  p.cancel()
  assert.equal(returns, 1)
})

test('opening editor uses one full-form page and saving refreshes only the edited card state', () => {
  const { instance: p, wx } = review()
  p.data.knowledgeDetail = setDetail()
  p.data.allQueue = [{ ...a, draft_unit_id: 'unit-a' }, { ...b, draft_unit_id: 'unit-b' }]
  p.data.activeCard = p.data.allQueue[0]
  p.data.answerResult = answered('a')
  p._cardStates = { a: { answer: '旧回答' }, b: { answer: '保留 B 的草稿' } }
  let options
  wx.navigateTo = (value) => { options = value }
  p.editKnowledgeUnit(event({ id: 'unit-a' }))
  assert.match(options.url, /pages\/knowledge-edit\/knowledge-edit/)
  const detail = setDetail()
  detail.units[0].question = '更新的问题'
  options.events.saved({ detail, unitId: 'unit-a' })
  assert.equal(p.data.knowledgeDetail.units[0].question, '更新的问题')
  assert.equal(p.data.answerResult, null)
  assert.equal(p._cardStates.a, undefined)
  assert.equal(p._cardStates.b.answer, '保留 B 的草稿')
})

async function settings(api = {}) {
  const context = page('reminders', {
    get: async (url) => url.endsWith('/preferences')
      ? { enabled: false, preferred_time: '20:00', daily_limit: 10, overdue_enabled: true, ai_evaluation_enabled: false }
      : { template_id: 'template-a', available_grants: 0 },
    ...api,
  })
  await context.instance.loadPreferences()
  return context
}

test('AI setting and study plan remain editable with reminders disabled; unsaved edits survive onShow', async () => {
  const { instance: p } = await settings()
  p.onAiEvaluationChange({ detail: { value: true } })
  p.onLimitChange({ detail: { value: 20 } })
  assert.equal(p.data.form.enabled, false)
  assert.equal(p.data.form.ai_evaluation_enabled, true)
  assert.equal(p.data.form.daily_limit, 20)
  p.loadPreferences = () => assert.fail('unsaved settings must not be reloaded')
  p.onShow()
  assert.equal(p.data.dirty, true)
})

test('settings save blocks concurrent edits and includes AI and reminder preferences', async () => {
  const pending = deferred()
  let payload
  const { instance: p } = await settings({ put: (_url, value) => { payload = value; return pending.promise } })
  p.onAiEvaluationChange({ detail: { value: true } })
  const saving = p.savePreferences()
  p.onAiEvaluationChange({ detail: { value: false } })
  assert.equal(p.data.form.ai_evaluation_enabled, true)
  assert.equal(payload.enabled, false)
  assert.equal(payload.ai_evaluation_enabled, true)
  pending.resolve(payload)
  await saving
  assert.equal(p.data.dirty, false)
  assert.equal(p.data.saved, true)
})

test('settings failure preserves edits and failed initial load cannot save defaults', async () => {
  const { instance: p } = await settings({ put: async () => { throw new Error('保存失败') } })
  p.onTimeChange({ detail: { value: '21:30' } })
  await p.savePreferences()
  assert.equal(p.data.form.preferred_time, '21:30')
  assert.equal(p.data.dirty, true)
  assert.equal(p.data.saved, false)
  const broken = await settings({
    get: async () => { throw new Error('读取失败') },
    put: () => assert.fail('must not save defaults after failed load'),
  })
  await broken.instance.savePreferences()
  assert.equal(broken.instance.data.preferencesAvailable, false)
})
