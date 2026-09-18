/**
 * `/api/v1` 的唯一客户端。
 *
 * 两条纪律（是从 2.7 那个静态页继承下来的，别在这里丢掉）：
 *
 *   1. **token 从 URL 拿**（`?token=`），之后所有请求带 Authorization 头。
 *      `EventSource` 发不了自定义头，所以 SSE 那条连接只能把 token 放查询串 ——
 *      这是唯一允许把 token 写进 URL 的地方，且它本来就来自 URL。
 *   2. **绝不拼外部地址**。`API` 是相对路径，请求只会打到同源的本地服务上。
 *      零出站不只是"没有 CDN"：一个拼出来的绝对地址同样是浏览器替这个工具
 *      发出去的请求。`check.py` 的 R7-ASSETS 会扫构建产物，扫的正是这类东西。
 */

import { ref } from 'vue'

const TOKEN_KEY = 'magpie.token'

function readToken() {
  if (typeof window === 'undefined') return ''
  const fromUrl = new URLSearchParams(window.location.search).get('token')
  if (fromUrl) {
    try {
      window.sessionStorage.setItem(TOKEN_KEY, fromUrl)
    } catch (err) {
      // 隐私模式下 sessionStorage 会抛。token 只活在这一次导航里也没关系。
    }
    return fromUrl
  }
  try {
    return window.sessionStorage.getItem(TOKEN_KEY) || ''
  } catch (err) {
    return ''
  }
}

export const API_PREFIX = '/api/v1'
export const token = ref(readToken())
export const hasToken = () => Boolean(token.value)

export class ApiError extends Error {
  constructor(status, payload) {
    const error = (payload && payload.error) || {}
    super(`${error.code || status}: ${error.message || '请求失败'}`)
    this.name = 'ApiError'
    this.status = status
    this.code = error.code || String(status)
    this.hint = error.hint || ''
    this.detail = error
  }
}

function authHeaders(extra) {
  return Object.assign({ Authorization: `Bearer ${token.value}` }, extra || {})
}

/** 一次 `/api/v1` 调用，返回**信封里的 data**。失败抛 `ApiError`。 */
export async function call(path, options = {}) {
  const response = await fetch(API_PREFIX + path, Object.assign({}, options, {
    headers: authHeaders(options.headers),
  }))
  let body = null
  try {
    body = await response.json()
  } catch (err) {
    body = null
  }
  if (!response.ok || !body || body.ok === false) {
    throw new ApiError(response.status, body)
  }
  return body.data
}

export const get = (path) => call(path)

export const post = (path, payload) => call(path, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(payload === undefined ? {} : payload),
})

export const patch = (path, payload) => call(path, {
  method: 'PATCH',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(payload === undefined ? {} : payload),
})

/** SSE 的 URL —— 唯一一处把 token 放进查询串的地方（EventSource 发不了头）。 */
export function eventsUrl(importId) {
  return `${API_PREFIX}/imports/${encodeURIComponent(importId)}/events` +
    `?token=${encodeURIComponent(token.value)}`
}

/**
 * 订阅一次导入的事件流。返回一个 `close()`。
 *
 * 批事件里带 `dropped` 计数：慢消费者丢帧是**允许**的（背压策略，见 2.7），
 * 但**丢帧不能静默** —— 这里把丢了多少条原样转给调用方，由界面说给用户听。
 */
export function watchImport(importId, onBatch, onOpen, onClose) {
  const source = new EventSource(eventsUrl(importId))
  let closed = false
  const close = () => {
    if (!closed) {
      closed = true
      source.close()
    }
  }
  source.addEventListener('open', () => onOpen && onOpen())
  source.addEventListener('batch', (event) => {
    let payload = null
    try {
      payload = JSON.parse(event.data)
    } catch (err) {
      return
    }
    onBatch(payload)
    if ((payload.events || []).some((e) => e.terminal)) close()
  })
  source.onerror = () => {
    close()
    onClose && onClose()
  }
  return close
}
