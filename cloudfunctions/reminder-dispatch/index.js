'use strict'

const http = require('http')
const https = require('https')
const cloud = require('wx-server-sdk')

cloud.init({ env: cloud.DYNAMIC_CURRENT_ENV })

const CLAIM_PATH = '/api/v1/internal/reminders/dispatch/claim'
const RESULT_PATH_PREFIX = '/api/v1/internal/reminders/dispatch'

class TimeoutError extends Error {
  constructor(message) {
    super(message)
    this.name = 'TimeoutError'
    this.code = 'BRIDGE_TIMEOUT'
  }
}

function positiveInteger(value, fallback, maximum) {
  const parsed = Number.parseInt(value, 10)
  if (!Number.isFinite(parsed) || parsed <= 0) return fallback
  return Math.min(parsed, maximum)
}

function requiredEnv(name) {
  const value = String(process.env[name] || '').trim()
  if (!value) throw new Error(`missing required environment variable: ${name}`)
  return value
}

function backendBaseUrl() {
  const raw = requiredEnv('MEMORY_AGENT_BASE_URL').replace(/\/+$/, '')
  const parsed = new URL(raw)
  if (!['http:', 'https:'].includes(parsed.protocol)) {
    throw new Error('MEMORY_AGENT_BASE_URL must use HTTP or HTTPS')
  }
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error('MEMORY_AGENT_BASE_URL must not contain credentials, query, or fragment')
  }
  return parsed.toString().replace(/\/+$/, '')
}

function templateFieldMap() {
  const raw = requiredEnv('WECHAT_TEMPLATE_FIELD_MAP_JSON')
  let parsed
  try {
    parsed = JSON.parse(raw)
  } catch (_error) {
    throw new Error('WECHAT_TEMPLATE_FIELD_MAP_JSON must be valid JSON')
  }
  if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') {
    throw new Error('WECHAT_TEMPLATE_FIELD_MAP_JSON must be a JSON object')
  }

  const entries = Object.entries(parsed)
  if (entries.length === 0) {
    throw new Error('WECHAT_TEMPLATE_FIELD_MAP_JSON must contain at least one field')
  }

  const targets = new Set()
  for (const [logicalField, templateField] of entries) {
    if (!logicalField.trim() || typeof templateField !== 'string' || !templateField.trim()) {
      throw new Error('template field mapping keys and values must be non-empty strings')
    }
    const normalizedTarget = templateField.trim()
    if (!/^[A-Za-z][A-Za-z0-9_]*\d+$/.test(normalizedTarget)) {
      throw new Error(`invalid WeChat template field name for ${logicalField}`)
    }
    if (targets.has(normalizedTarget)) {
      throw new Error(`duplicate WeChat template field mapping: ${normalizedTarget}`)
    }
    targets.add(normalizedTarget)
  }
  return Object.fromEntries(
    entries.map(([logicalField, templateField]) => [logicalField.trim(), templateField.trim()])
  )
}

function requestJson(url, { method = 'POST', headers = {}, body, timeoutMs }) {
  return new Promise((resolve, reject) => {
    const target = new URL(url)
    const transport = target.protocol === 'https:' ? https : http
    const payload = body === undefined ? null : Buffer.from(JSON.stringify(body), 'utf8')
    const request = transport.request(
      target,
      {
        method,
        headers: {
          accept: 'application/json',
          ...(payload ? { 'content-type': 'application/json', 'content-length': payload.length } : {}),
          ...headers,
        },
      },
      (response) => {
        const chunks = []
        response.on('data', (chunk) => chunks.push(chunk))
        response.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8')
          let data = null
          if (text) {
            try {
              data = JSON.parse(text)
            } catch (_error) {
              const error = new Error(`backend returned non-JSON response (${response.statusCode})`)
              error.statusCode = response.statusCode
              reject(error)
              return
            }
          }
          if (response.statusCode < 200 || response.statusCode >= 300) {
            const detail = data && (data.detail || data.message)
            const error = new Error(
              detail ? `backend request failed (${response.statusCode}): ${String(detail)}` : `backend request failed (${response.statusCode})`
            )
            error.statusCode = response.statusCode
            reject(error)
            return
          }
          resolve(data)
        })
      }
    )

    request.setTimeout(timeoutMs, () => {
      request.destroy(new TimeoutError(`backend request timed out after ${timeoutMs}ms`))
    })
    request.on('error', reject)
    if (payload) request.write(payload)
    request.end()
  })
}

function withTimeout(promise, timeoutMs, label) {
  let timer
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(
      () => reject(new TimeoutError(`${label} timed out after ${timeoutMs}ms`)),
      timeoutMs
    )
  })
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer))
}

function jobId(job) {
  return String(job.id || job.job_id || '').trim()
}

function normalizeTemplateValue(rawValue, logicalField) {
  const value =
    rawValue && typeof rawValue === 'object' && !Array.isArray(rawValue)
      ? rawValue.value
      : rawValue
  if (value === undefined || value === null) {
    throw new Error(`job is missing template data field: ${logicalField}`)
  }
  return { value: String(value) }
}

function mapTemplateData(job, fieldMap) {
  const logicalData = job.data || job.template_data
  if (!logicalData || Array.isArray(logicalData) || typeof logicalData !== 'object') {
    throw new Error('job data must be an object of logical template fields')
  }
  const mapped = {}
  for (const [logicalField, templateField] of Object.entries(fieldMap)) {
    mapped[templateField] = normalizeTemplateValue(logicalData[logicalField], logicalField)
  }
  return mapped
}

function normalizedWechatResponse(response) {
  if (!response || typeof response !== 'object') return null
  return {
    errcode: response.errCode ?? response.errcode ?? null,
    errmsg: response.errMsg ?? response.errmsg ?? null,
  }
}

function errorMetadata(error) {
  const providerCode = error && (error.errCode ?? error.errcode)
  const errcode = providerCode === undefined || providerCode === null
    ? null
    : Number(providerCode)
  const errmsg = String(
    (error && (error.errMsg || error.errmsg || error.message)) || 'unknown WeChat send error'
  ).slice(0, 500)
  return {
    errcode: Number.isFinite(errcode) ? errcode : null,
    errmsg,
  }
}

function isTimeout(error) {
  return error instanceof TimeoutError || (error && error.code === 'BRIDGE_TIMEOUT')
}

async function claimJobs(baseUrl, token, timeoutMs, batchSize, leaseSeconds) {
  const response = await requestJson(`${baseUrl}${CLAIM_PATH}`, {
    headers: { 'X-Reminder-Dispatch-Token': token },
    body: { batch_size: batchSize, lease_seconds: leaseSeconds },
    timeoutMs,
  })
  const jobs = Array.isArray(response) ? response : response && response.jobs
  if (!Array.isArray(jobs)) throw new Error('claim response must be an array or contain jobs[]')
  return jobs
}

async function reportResult(baseUrl, token, timeoutMs, id, result) {
  return requestJson(`${baseUrl}${RESULT_PATH_PREFIX}/${encodeURIComponent(id)}/result`, {
    headers: { 'X-Reminder-Dispatch-Token': token },
    body: result,
    timeoutMs,
  })
}

async function sendOne(job, fieldMap, sendTimeoutMs) {
  const id = jobId(job)
  if (!id) throw new Error('claimed job is missing id')

  const openid = String(job.openid || '').trim()
  const templateId = String(job.template_id || job.templateId || '').trim()
  if (!openid) throw new Error('claimed job is missing openid')
  if (!templateId) throw new Error('claimed job is missing template_id')

  const request = {
    touser: openid,
    templateId,
    data: mapTemplateData(job, fieldMap),
    miniprogramState: String(process.env.WECHAT_MINIPROGRAM_STATE || 'developer').trim(),
    lang: String(process.env.WECHAT_SUBSCRIBE_LANG || 'zh_CN').trim(),
  }
  const page = String(job.page || process.env.WECHAT_SUBSCRIBE_PAGE || '').trim()
  if (page) request.page = page

  try {
    const response = await withTimeout(
      cloud.openapi.subscribeMessage.send(request),
      sendTimeoutMs,
      'WeChat subscribe message send'
    )
    const provider = normalizedWechatResponse(response)
    if (provider && provider.errcode !== null && Number(provider.errcode) !== 0) {
      return {
        status: 'failed',
        wechat_errcode: Number(provider.errcode),
        wechat_errmsg: String(provider.errmsg || 'WeChat rejected the message').slice(0, 500),
        response: provider,
      }
    }
    return {
      status: 'sent',
      wechat_errcode: provider && provider.errcode !== null ? Number(provider.errcode) : null,
      wechat_errmsg: provider && provider.errmsg ? String(provider.errmsg).slice(0, 500) : null,
      response: provider,
    }
  } catch (error) {
    const metadata = errorMetadata(error)
    // A timeout or an SDK/transport error without a WeChat error code may have
    // reached WeChat. Mark it uncertain and never resend it from this bridge.
    const status = isTimeout(error) || metadata.errcode === null ? 'uncertain' : 'failed'
    return {
      status,
      wechat_errcode: metadata.errcode,
      wechat_errmsg: metadata.errmsg,
      response: null,
    }
  }
}

exports.main = async (_event, _context) => {
  const baseUrl = backendBaseUrl()
  const token = requiredEnv('MEMORY_AGENT_REMINDER_TOKEN')
  const fieldMap = templateFieldMap()
  const httpTimeoutMs = positiveInteger(process.env.MEMORY_AGENT_HTTP_TIMEOUT_MS, 10000, 60000)
  const sendTimeoutMs = positiveInteger(process.env.WECHAT_SEND_TIMEOUT_MS, 10000, 60000)
  const batchSize = positiveInteger(process.env.REMINDER_DISPATCH_BATCH_SIZE, 10, 100)
  const leaseSeconds = positiveInteger(process.env.REMINDER_DISPATCH_LEASE_SECONDS, 120, 900)

  const jobs = await claimJobs(baseUrl, token, httpTimeoutMs, batchSize, leaseSeconds)
  const summary = { claimed: jobs.length, sent: 0, failed: 0, uncertain: 0, callback_failed: 0 }

  for (const job of jobs) {
    const id = jobId(job)
    let result
    try {
      result = await sendOne(job, fieldMap, sendTimeoutMs)
    } catch (error) {
      const metadata = errorMetadata(error)
      result = {
        status: 'failed',
        wechat_errcode: null,
        wechat_errmsg: metadata.errmsg,
        response: { bridge_error_code: 'BRIDGE_INVALID_JOB' },
      }
    }

    summary[result.status] += 1
    try {
      await reportResult(baseUrl, token, httpTimeoutMs, id, {
        ...result,
        claim_token: job.claim_token || job.lease_token || null,
      })
    } catch (error) {
      summary.callback_failed += 1
      console.error('[reminder-dispatch] result callback failed', {
        job_id: id,
        status: result.status,
        error: String(error.message || error).slice(0, 300),
      })
    }
  }

  console.log('[reminder-dispatch] completed', summary)
  return summary
}
