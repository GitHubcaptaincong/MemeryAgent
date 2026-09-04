const api = require('../../utils/api')
const { idempotencyKey } = require('../../utils/ids')

const SUBSCRIPTION_RESULT_MESSAGES = {
  accept: {
    type: 'success',
    message: '已获得一次提醒授权。',
  },
  acceptWithAudio: {
    type: 'success',
    message: '已获得一次提醒授权。',
  },
  reject: {
    type: 'warning',
    message: '这次没有授权提醒。',
  },
  ban: {
    type: 'warning',
    message: '订阅消息已被关闭，请到小程序设置中开启。',
  },
  filter: {
    type: 'warning',
    message: '微信过滤了这次订阅授权，可稍后重试。',
  },
}

function sentAtLabel(value) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return `${date.getMonth() + 1} 月 ${date.getDate()} 日 ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
}

function localTimezone() {
  try {
    if (typeof Intl !== 'undefined' && Intl.DateTimeFormat) {
      const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone
      if (timezone === 'UTC' || (typeof timezone === 'string' && timezone.includes('/'))) {
        return timezone
      }
    }
  } catch (error) {
    // Some older WeChat runtimes do not provide full Intl timezone data.
  }
  return 'Asia/Shanghai'
}

Page({
  data: {
    loading: true,
    saving: false,
    subscribing: false,
    saved: false,
    error: '',
    form: {
      enabled: true,
      preferred_time: '20:00',
      daily_limit: 10,
      overdue_enabled: true,
      ai_evaluation_enabled: true,
      timezone: 'Asia/Shanghai',
    },
    subscription: {
      template_id: null,
      delivery_enabled: false,
      available_grants: 0,
      last_delivery_status: null,
      last_sent_at: null,
      last_sent_label: '',
    },
    subscriptionStatusAvailable: true,
    subscriptionStatusError: '',
    subscriptionFeedback: '',
    subscriptionFeedbackType: 'info',
    subscriptionAuthorizationEnabled: false,
  },

  onShow() {
    const tabBar = typeof this.getTabBar === 'function' && this.getTabBar()
    if (tabBar) tabBar.setData({ selected: 2 })
    this.loadPreferences()
  },

  onPullDownRefresh() {
    this.loadPreferences().finally(() => wx.stopPullDownRefresh())
  },

  async loadPreferences() {
    this.setData({ loading: true, error: '' })
    try {
      const [form, subscriptionResult] = await Promise.all([
        api.get('/reminders/preferences'),
        api.get('/reminders/status')
          .then((value) => ({ value }))
          .catch((error) => ({ error })),
      ])
      const subscriptionStatusAvailable = !subscriptionResult.error
      const subscription = subscriptionStatusAvailable
        ? subscriptionResult.value
        : this.data.subscription
      const timezone = localTimezone()
      this.setData({
        loading: false,
        form: { ...form, timezone },
        subscription: { ...subscription, last_sent_label: sentAtLabel(subscription.last_sent_at) },
        subscriptionStatusAvailable,
        subscriptionStatusError: subscriptionStatusAvailable ? '' : subscriptionResult.error.message,
        subscriptionAuthorizationEnabled: subscriptionStatusAvailable && Boolean(subscription.template_id),
      })
    } catch (error) {
      this.setData({
        loading: false,
        error: error.message,
        subscriptionStatusAvailable: false,
        subscriptionStatusError: error.message,
        subscriptionAuthorizationEnabled: false,
      })
    }
  },

  onEnabledChange(event) {
    this.setData({ 'form.enabled': event.detail.value, saved: false })
  },

  onTimeChange(event) {
    this.setData({ 'form.preferred_time': event.detail.value, saved: false })
  },

  onOverdueChange(event) {
    this.setData({ 'form.overdue_enabled': event.detail.value, saved: false })
  },

  onAiEvaluationChange(event) {
    this.setData({ 'form.ai_evaluation_enabled': event.detail.value, saved: false })
  },

  onLimitChanging(event) {
    const dailyLimit = Math.max(1, Math.min(100, Number(event.detail.value)))
    this.setData({ 'form.daily_limit': dailyLimit, saved: false })
  },

  onLimitChange(event) {
    const dailyLimit = Math.max(1, Math.min(100, Number(event.detail.value)))
    this.setData({ 'form.daily_limit': dailyLimit, saved: false })
  },

  async savePreferences() {
    if (this.data.saving) return
    this.setData({ saving: true, saved: false, error: '' })
    try {
      const form = await api.put('/reminders/preferences', {
        enabled: this.data.form.enabled,
        preferred_time: this.data.form.preferred_time,
        daily_limit: this.data.form.daily_limit,
        overdue_enabled: this.data.form.overdue_enabled,
        ai_evaluation_enabled: this.data.form.ai_evaluation_enabled,
        timezone: this.data.form.timezone,
      })
      this.setData({ saving: false, saved: true, form })
      wx.showToast({ title: '设置已保存', icon: 'success' })
    } catch (error) {
      this.setData({ saving: false, error: error.message })
    }
  },

  requestSubscription() {
    if (this.data.subscribing) return
    if (!this.data.subscriptionStatusAvailable) {
      this.setData({
        error: this.data.subscriptionStatusError || '订阅状态读取失败，请重新进入或下拉刷新。',
      })
      return
    }
    const templateId = this.data.subscription.template_id
    if (!templateId) {
      this.setData({ error: '服务端尚未配置微信订阅消息模板 ID。' })
      return
    }
    if (typeof wx === 'undefined' || typeof wx.requestSubscribeMessage !== 'function') {
      this.setData({ error: '当前微信基础库不支持订阅消息授权。' })
      return
    }
    this.setData({ subscribing: true, error: '' })
    // This call must remain directly inside the tap handler; WeChat requires a user gesture.
    try {
      wx.requestSubscribeMessage({
        tmplIds: [templateId],
        success: async (result) => {
          const decision = result[templateId]
          const resultFeedback = SUBSCRIPTION_RESULT_MESSAGES[decision]
          if (!resultFeedback) {
            this.setData({
              subscribing: false,
              error: '没有收到有效的授权结果，请重试。',
              subscriptionFeedback: '微信未返回可提交的授权结果。',
              subscriptionFeedbackType: 'error',
            })
            return
          }
          try {
            const subscription = await api.post('/reminders/subscription-grants', {
              template_id: templateId,
              result: decision,
              idempotency_key: idempotencyKey('mini-subscribe'),
            })
            this.setData({
              subscribing: false,
              subscription: { ...subscription, last_sent_label: sentAtLabel(subscription.last_sent_at) },
              subscriptionAuthorizationEnabled: Boolean(subscription.template_id),
              subscriptionFeedback: resultFeedback.message,
              subscriptionFeedbackType: resultFeedback.type,
              error: '',
            })
            if (decision === 'accept' || decision === 'acceptWithAudio') {
              wx.showToast({ title: '已获得一次提醒授权', icon: 'success' })
            }
          } catch (error) {
            this.setData({
              subscribing: false,
              error: error.message,
              subscriptionFeedback: '授权结果提交失败，请稍后重试。',
              subscriptionFeedbackType: 'error',
            })
          }
        },
        fail: (error) => {
          this.setData({
            subscribing: false,
            error: error.message,
            subscriptionFeedback: '微信订阅授权请求失败，请稍后重试。',
            subscriptionFeedbackType: 'error',
          })
        },
      })
    } catch (error) {
      this.setData({
        subscribing: false,
        error: error.message || '请求订阅授权失败',
        subscriptionFeedback: '微信订阅授权请求失败，请稍后重试。',
        subscriptionFeedbackType: 'error',
      })
    }
  },
})
