/**
 * 聚合卡下钻（评审二/三：「domain × 30」点开要看见那 30 个域名）。
 *
 * 与 aggregate.test.js 同一条集成路径（fetch → store → visible），钉的是
 * **下钻的三段语义**：
 *
 *   1. 下钻 = 服务端 `kinds=<kind>` 重载（拿到的是库里这一类的前 500 个，
 *      不是已载入窗口里恰好摊到的几个）+ `_drillKind` 旁路聚合（5,593 个
 *      domain 也不许再聚回一张卡 —— 那等于点了没反应）。
 *   2. 退出 = 状态、筛选、图三样一起复原。
 *   3. **手动改筛选 = 退出下钻**（`toggleKind` / `clearFilter` 顺带清标记）：
 *      用户已经改了主意，留着下钻状态只会让"我明明点了取消怎么还是单类型
 *      视图"变成一个谜。
 *
 * 聚合卡点击 → `drillKind` 的接线在 GraphCanvas（onNodeClick 对 `kind:`
 * 前缀的早退改成了调它），这里从 store 层钉语义 —— 与 aggregate 的分工
 * 正好互补（它管"什么时候聚合"，这里管"聚了之后怎么钻下去"）。
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

/** 只会回答 `/api/v1/graph` 的桩（查询串**原样记录**，断言 kinds= 靠它）。 */
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

describe('聚合卡下钻（评审二/三）', () => {
  beforeEach(() => {
    vi.resetModules()
  })

  it('下钻：聚合展开为逐实体、kinds 进 URL；退出三样一起复原', async () => {
    const calls = []
    stubFetch(fakeGraph(300), calls)
    const { store } = await import('../src/store.js')
    await store.loadGraph()
    expect(store.visible.aggregated).toBe(true)         // 300 > 200 → 聚合

    await store.drillKind('domain')

    expect(store.drill).toBe('domain')
    expect(store.filter.kinds).toEqual(['domain'])
    expect(calls.at(-1)).toContain('kinds=domain')      // 服务端重载，不是本地演
    // 150 个 domain **逐个**渲染：不许再聚回一张卡（点了没反应就是这么来的）
    expect(store.visible.aggregated).toBe(false)
    expect(store.visible.nodes).toHaveLength(150)
    for (const node of store.visible.nodes) {
      expect(String(node.id)).not.toMatch(/^kind:/)
      expect(node.data.aggregated).toBeUndefined()
    }
    // 下钻动作进活动日志（"画布怎么突然变了"要有迹可查）
    expect(store.logLines.some((line) => line.text.includes('下钻 domain'))).toBe(true)

    await store.exitDrill()

    expect(store.drill).toBe(null)
    expect(store.filter.kinds).toEqual([])
    expect(calls.at(-1)).not.toContain('kinds=')        // 图重新全量载入
    expect(store.visible.aggregated).toBe(true)         // 300 → 回到聚合
    expect(store.visible.nodes).toHaveLength(2)         // domain / ip 两张卡
  })

  it('手动改筛选（toggleKind）= 退出下钻 —— 标记与单类型视图一起消失', async () => {
    const calls = []
    stubFetch(fakeGraph(300), calls)
    const { store } = await import('../src/store.js')
    await store.loadGraph()

    await store.drillKind('domain')
    expect(store.drill).toBe('domain')

    await store.toggleKind('ip')                        // 手动把 ip 加进来
    expect(store.drill).toBe(null)                      // 下钻标记被顺带清掉
    expect(store.filter.kinds).toEqual(['domain', 'ip'])
    // 两类合起来 300 > 200 → 按正常规则重新聚合（用户已经改了主意，
    // "还是单类型视图"就是个谜了）
    expect(store.visible.aggregated).toBe(true)
  })

  it('clearFilter 也退出下钻，且真的重新载图', async () => {
    const calls = []
    stubFetch(fakeGraph(300), calls)
    const { store } = await import('../src/store.js')
    await store.loadGraph()

    await store.drillKind('domain')
    expect(store.drill).toBe('domain')

    await store.clearFilter()
    expect(store.drill).toBe(null)
    expect(store.filter.kinds).toEqual([])
    expect(calls.at(-1)).not.toContain('kinds=')
    expect(store.visible.aggregated).toBe(true)
  })

  it('空 kind / 假值不发请求也不改状态（防御分支）', async () => {
    const calls = []
    stubFetch(fakeGraph(300), calls)
    const { store } = await import('../src/store.js')
    await store.loadGraph()
    const before = calls.length

    await store.drillKind('')
    await store.drillKind(null)

    expect(store.drill).toBe(null)
    expect(calls.length).toBe(before)                   // 一次网络都没花
    expect(store.filter.kinds).toEqual([])
  })
})
