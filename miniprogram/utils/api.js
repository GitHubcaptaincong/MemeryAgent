const config = require('../config')

function normalizeError(payload, statusCode) {
  if (payload && typeof payload.detail === 'string') return payload.detail
  if (payload && payload.detail && payload.detail.message) return payload.detail.message
  if (payload && payload.detail) return JSON.stringify(payload.detail)
  return `请求失败（${statusCode || '网络异常'}）`
}

function localRequest(path, options) {
  return new Promise((resolve, reject) => {
    const task = wx.request({
      url: `${config.apiBaseUrl}${path}`,
      method: options.method || 'GET',
      data: options.data,
      timeout: options.timeout || config.requestTimeout,
      header: {
        'content-type': 'application/json',
        ...(options.header || {}),
      },
      success(response) {
        if (response.statusCode >= 200 && response.statusCode < 300) {
          resolve(response.data)
          return
        }
        reject(new Error(normalizeError(response.data, response.statusCode)))
      },
      fail(error) {
        reject(new Error(error.errMsg || '无法连接后端服务'))
      },
    })
    if (typeof options.onTask === 'function') options.onTask(task)
  })
}

function cloudRequest(path, options) {
  return new Promise((resolve, reject) => {
    if (!wx.cloud || !wx.cloud.callContainer) {
      reject(new Error('当前基础库不支持云托管调用'))
      return
    }
    const task = wx.cloud.callContainer({
      config: { env: config.cloudEnv },
      service: config.cloudService,
      path: `${config.apiPrefix}${path}`,
      method: options.method || 'GET',
      data: options.data,
      header: {
        'X-WX-SERVICE': config.cloudService,
        'content-type': 'application/json',
        ...(options.header || {}),
      },
      success(response) {
        const statusCode = response.statusCode || 200
        if (statusCode >= 200 && statusCode < 300) {
          resolve(response.data)
          return
        }
        reject(new Error(normalizeError(response.data, statusCode)))
      },
      fail(error) {
        reject(new Error(error.errMsg || '云托管请求失败'))
      },
    })
    if (typeof options.onTask === 'function') options.onTask(task)
  })
}

function request(path, options = {}) {
  return config.useCloud ? cloudRequest(path, options) : localRequest(path, options)
}

module.exports = {
  request,
  get(path, options = {}) {
    return request(path, { ...options, method: 'GET' })
  },
  post(path, data, options = {}) {
    return request(path, { ...options, method: 'POST', data })
  },
  put(path, data, options = {}) {
    return request(path, { ...options, method: 'PUT', data })
  },
  patch(path, data, options = {}) {
    return request(path, { ...options, method: 'PATCH', data })
  },
  delete(path, options = {}) {
    return request(path, { ...options, method: 'DELETE' })
  },
}
