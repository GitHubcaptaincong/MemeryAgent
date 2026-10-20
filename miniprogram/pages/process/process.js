const api = require('../../utils/api')
const { stateLabel } = require('../../utils/format')
const { processHistory } = require('../../utils/run-history')

Page({
  data: { loading: true, error: '', run: null, stateLabel: '', stages: [], events: [], expanded: false },
  onLoad(options) {
    this.runId = options.runId
    this.reload()
  },
  onUnload() { this.unloaded = true },
  async reload() {
    if (!this.runId || this.fetching) return
    this.fetching = true
    this.setData({ loading: true, error: '' })
    try {
      const [run, events] = await Promise.all([
        api.get(`/runs/${encodeURIComponent(this.runId)}`),
        api.get(`/runs/${encodeURIComponent(this.runId)}/events?after_seq=0`),
      ])
      if (!this.unloaded) this.setData({ run, stateLabel: stateLabel(run.state), ...processHistory(run, events), loading: false })
    } catch (error) {
      if (!this.unloaded) this.setData({ loading: false, error: error.message })
    } finally { this.fetching = false }
  },
  toggleEvents() { this.setData({ expanded: !this.data.expanded }) },
})
