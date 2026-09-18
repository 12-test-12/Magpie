/**
 * 聚合视图的**集成**测试：从 `fetch` 一路走到画布要渲染的节点。
 *
 * 为什么单靠 `graph.test.js` 不够：那里验的是 `buildAggregate` 这个函数，
 * 而真实路径上还有三道关 —— `api.js` 拆信封、`store.loadGraph` 写状态、
 * `store.visible` 决定"渲染什么"。第一版就是在 `buildAggregate` 里漏了
 * `node.data`，而单测只断言了 `id` 前缀，于是**聚合视图下每张卡片都拿到
 * undefined** —— 开发时数据少、从没进过聚合，谁也不会发现。
 *
 * 这里把 `fetch` 换成桩，喂 300 个节点，然后断言画布真的拿到的是"类型卡片"
 * 而不是 300 个什么都没有的壳。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

function fakeGraph(count) {
  const nodes = []
  for (let i = 1; i <= count; i += 1) {
    nodes.push({ id: i, kind: i % 2 ? 'domain' : 'ip', lifecycle: 'active',
                 entity_key: `h${i}.example.com` })
  }
  const edges = []
  for (let i = 1; i < count; i += 1) {
    edges.push({ id: i, src: i, dst: i + 1, rel: 'resolves_to', confidence: 'high',
                 src_key: `h${i}.example.com`, dst_key: `h${i + 1}.example.com` })
  }
  return { nodes, edges, layout: {}, project_id: 1 }
}

/** 一个只会回答 `/api/v1/graph` 的桩。其它端点直接抛 —— 用不到就是不该被调到。 */
function stubFetch(graph, calls) {
  global.fetch = vi.fn(async (url, options) => {
    calls.push(String(url))
    if (String(url).includes('/graph')) {
      return { ok: true, status: 200,
               json: async () => ({ ok: true, data: graph, meta: {} }) }
    }
    throw new Error(`这个测试不该请求 ${url}（${options?.method || 'GET'}）`)
  })
}

describe('> 200 个节点自动进聚合视图', () => {
  beforeEach(() => {
    vi.resetModules()
  })

  it('300 个节点渲染成「类型」卡片，且卡片形状完整', async () => {
    const calls = []
    stubFetch(fakeGraph(300), calls)
    const { store } = await import('../src/store.js')
    await store.loadGraph()

    expect(store.counts.entities).toBe(300)
    expect(store.visible.aggregated).toBe(true)
    expect(store.visible.nodes).toHaveLength(2)          // domain / ip
    for (const node of store.visible.nodes) {
      expect(node.data).toBeTypeOf('object')
      expect(node.data.aggregated).toBe(true)
      expect(node.data.count).toBe(150)
      expect(node.position).toBeTypeOf('object')
      expect(node.id).toMatch(/^kind:/)
    }
    expect(store.visible.edges.length).toBeGreaterThan(0)
    expect(store.visible.edges[0].data.rel).toBe('resolves_to')
    // 只打了一次图端点 —— 没有 N+1
    expect(calls.filter((u) => u.includes('/graph'))).toHaveLength(1)
  })

  it('阈值以内的节点仍然逐个渲染', async () => {
    stubFetch(fakeGraph(20), [])
    const { store } = await import('../src/store.js')
    await store.loadGraph()

    expect(store.visible.aggregated).toBe(false)
    expect(store.visible.nodes).toHaveLength(20)
    expect(store.visible.nodes[0].data.key).toBe('h1.example.com')
    expect(store.visible.nodes[0].data.aggregated).toBeUndefined()
  })

  it('按类型筛选之后节点数掉到阈值以下，就退回逐个渲染', async () => {
    stubFetch(fakeGraph(300), [])
    const { store } = await import('../src/store.js')
    await store.loadGraph()
    expect(store.visible.aggregated).toBe(true)

    store.toggleKind('domain')                            // 只剩 150 个
    expect(store.visible.nodes).toHaveLength(150)
    expect(store.visible.aggregated).toBe(false)
    expect(store.visible.nodes[0].data).toBeTypeOf('object')
  })

  it('图端点报错时画布拿到空图与一条**说出来**的错误，而不是静默空白', async () => {
    global.fetch = vi.fn(async () => ({
      ok: false, status: 500,
      json: async () => ({ ok: false, error: { code: 'E_DB', message: '数据库错误' } }),
    }))
    const { store } = await import('../src/store.js')
    await store.loadGraph()
    expect(store.loadError).toContain('E_DB')
    expect(store.visible.nodes).toHaveLength(0)
    expect(store.loading).toBe(false)
  })
})
