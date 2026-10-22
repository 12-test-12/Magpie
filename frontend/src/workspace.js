import { get } from './api.js'

export const workspacePages = {
  assets: { label: '资产', english: 'ASSET INVENTORY', hint: '按主机聚合域名、IP 与网段，沿来源检查研究对象。' },
  services: { label: '服务与中间件', english: 'SERVICES', hint: '查看已观察到的服务与产品；未关联主机的对象单独分组。' },
  api: { label: 'API 接口', english: 'API EXPLORER', hint: '从主机到路径分组，筛选已导入的接口观测。' },
  js: { label: 'JS 资源', english: 'JAVASCRIPT RESOURCES', hint: '查看已导入资源内容版本与保守静态调用候选。' },
  findings: { label: '漏洞与发现', english: 'VULNERABILITY FINDINGS', hint: '统一管理扫描器命中与人工安全发现；涵盖处置研判、主重复去重与复测生命周期。' },
  evidence: { label: 'Evidence 证据', english: 'EVIDENCE LIBRARY', hint: '包含历史证据。仅受管归档支持脱敏预览；原始定位与哈希始终可查。' },
  notes: { label: 'Notes 笔记', english: 'RESEARCH NOTES', hint: '保留原始贡献，维护人工修订、归档与研究证据引用。' },
}

// Session navigation memory, bounded to the most recent project/page contexts.
// It contains filters and position only; never facts or copies of backend data.
export const workspaceMemory = new Map()
export const apiResearchDefaults = () => ({ parameter_name: '', parameter_location: '', response_status: null,
  response_mime: '', credential: '', api_origin: '' })
export function remember(key, value) {
  workspaceMemory.delete(key)
  // Navigation values are JSON primitives; JSON also unwraps nested Vue proxies.
  workspaceMemory.set(key, JSON.parse(JSON.stringify(value)))
  if (workspaceMemory.size > 24) workspaceMemory.delete(workspaceMemory.keys().next().value)
}

export function params(values) {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== null) query.set(key, String(value))
  }
  return query.toString()
}

// Every owner gets its own request slot. Replacement/unmount cancels fetch;
// generation checks also guard mocks/transports that fail to honor AbortSignal.
export function requestSlot() {
  let controller, generation = 0
  return {
    cancel() { generation++; controller?.abort() },
    async run(path, apply, fail, done = () => {}) {
      const current = ++generation
      controller?.abort()
      controller = new AbortController()
      const active = controller
      const timer = setTimeout(() => active.abort('timeout'), 15000)
      try {
        const result = await get(path, { signal: active.signal })
        if (current === generation) apply(result)
      } catch (error) {
        if (current === generation) fail(active.signal.aborted ? '请求超时，请重试。' : error.message)
      } finally {
        clearTimeout(timer)
        if (current === generation) done()
      }
    },
  }
}
