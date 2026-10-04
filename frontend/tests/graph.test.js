/**
 * 画布纯逻辑的单测。**这里验的是 2.8 交付标准表里最难用眼睛验的那几行。**
 *
 *   性能  ——「1000 节点保持 55fps」没办法在 jsdom 里量 fps（没有合成器）。
 *            能验、也真正决定 fps 的是**渲染多少个元素**：聚合阈值以上画布
 *            只渲染"类型"而不是"个体"。把 1000 个节点丢进来断言渲染数 ≤ 阈值，
 *            量的是那条承诺的**支点**，而不是一个漂亮的数字。
 *   防混乱 ——「> 200 节点自动进聚合视图」与「卡片尺寸固定」「布局持久化」。
 *   只读   —— 图的内容**只能**来自 `GET /graph`。这个模块里所有函数的输出
 *            集合都是输入集合的子集 —— 没有一条路能产出新的节点 id 或新的边。
 */

import { describe, expect, it } from 'vitest'
import {
  AGGREGATE_THRESHOLD,
  NODE_HEIGHT,
  NODE_WIDTH,
  applyLayout,
  buildAggregate,
  degreeOf,
  filterGraph,
  kindColor,
  neighbourhood,
  relationColor,
  shouldAggregate,
  toFlow,
} from '../src/graph.js'

function makeGraph(n, edges = []) {
  const nodes = []
  for (let i = 1; i <= n; i += 1) {
    nodes.push({ id: i, kind: i % 3 === 0 ? 'service' : (i % 3 === 1 ? 'domain' : 'ip'),
                 lifecycle: 'active', entity_key: `host-${i}.example.com` })
  }
  const list = edges.slice()
  for (let i = 1; i < n; i += 1) {
    list.push({ id: i, src: i, dst: i + 1, rel: 'resolves_to', confidence: 'high',
                src_key: `host-${i}.example.com`, dst_key: `host-${i + 1}.example.com` })
  }
  return { nodes, edges: list, layout: {} }
}

describe('聚合（防视觉混乱第 ② 条）', () => {
  it('阈值以下不聚合，阈值以上聚合', () => {
    expect(shouldAggregate(makeGraph(AGGREGATE_THRESHOLD).nodes)).toBe(false)
    expect(shouldAggregate(makeGraph(AGGREGATE_THRESHOLD + 1).nodes)).toBe(true)
  })

  it('1000 个节点只渲染出「类型」那么多张卡片', () => {
    const graph = makeGraph(1000)
    const aggregated = buildAggregate(graph.nodes, graph.edges)
    // 3 种 kind → 3 张卡片。这是"1000 节点还能跑满帧"的实际口径：
    // 不是让 1000 个 DOM 节点飞起来，而是从来不同时渲染 1000 个。
    expect(aggregated.nodes).toHaveLength(3)
    expect(aggregated.edges.length).toBeLessThanOrEqual(9)
    expect(aggregated.groups.reduce((sum, g) => sum + g.count, 0)).toBe(1000)
  })

  it('聚合不丢边界信息：每个类型的数量都对得上', () => {
    const graph = makeGraph(999)
    const aggregated = buildAggregate(graph.nodes, graph.edges)
    const byKind = new Map(aggregated.groups.map((g) => [g.kind, g.count]))
    expect(byKind.get('domain') + byKind.get('ip') + byKind.get('service')).toBe(999)
  })

  it('聚合节点 id 带 kind: 前缀，与实体 id 不可能撞号', () => {
    const aggregated = buildAggregate(makeGraph(300).nodes, [])
    for (const node of aggregated.nodes) expect(node.id).toMatch(/^kind:/)
  })

  it('拖过的聚合卡留在保存位置，其余仍按圆环', () => {
    const graph = makeGraph(300)
    const base = buildAggregate(graph.nodes, graph.edges)
    const saved = { 'kind:domain': { x: 42, y: 24 } }
    const kept = buildAggregate(graph.nodes, graph.edges, { saved })
    const domainCard = kept.nodes.find((n) => n.id === 'kind:domain')
    const ipCard = kept.nodes.find((n) => n.id === 'kind:ip')
    expect(domainCard.position).toEqual({ x: 42, y: 24 })
    // 没拖过的那张不受影响 —— saved 只覆盖命中的卡
    expect(ipCard.position).toEqual(base.nodes.find((n) => n.id === 'kind:ip').position)
  })

  it('聚合节点与边带齐 Vue Flow 需要的形状（data / position / source / target）', () => {
    // 这条是"聚合视图从没被打开过"那个盲区的补丁：卡片组件读 `data.*`、
    // Vue Flow 读 `position` / `source` / `target`，缺一个就是白屏或控制台报错，
    // 而开发时数据少、永远进不了聚合，于是没人发现。
    const aggregated = buildAggregate(makeGraph(400).nodes, makeGraph(400).edges)
    for (const node of aggregated.nodes) {
      expect(node.position).toBeTypeOf('object')
      expect(node.data).toBeTypeOf('object')
      for (const field of ['kind', 'key', 'count', 'degree', 'collapsed']) {
        expect(node.data).toHaveProperty(field)
      }
      expect(node.data.aggregated).toBe(true)
    }
    expect(aggregated.edges.length).toBeGreaterThan(0)
    for (const edge of aggregated.edges) {
      expect(edge.source).toMatch(/^kind:/)
      expect(edge.target).toMatch(/^kind:/)
      expect(edge.data).toBeTypeOf('object')
      expect(edge.data.rel).toBeTypeOf('string')
    }
  })
})

describe('布局（防视觉混乱第 ① ③ 条）', () => {
  it('卡片尺寸是常量 —— 与内容长度无关', () => {
    expect(NODE_WIDTH).toBeGreaterThan(0)
    expect(NODE_HEIGHT).toBeGreaterThan(0)
  })

  it('持久化过的位置不会被动', () => {
    const nodes = makeGraph(5).nodes
    const saved = { 3: { x: 777, y: 888 } }
    const positions = applyLayout(nodes, saved)
    expect(positions.get('3')).toEqual({ x: 777, y: 888 })
  })

  it('同样的输入出同样的位置（确定性）', () => {
    const nodes = makeGraph(40).nodes
    const shuffled = [...nodes].reverse()
    const a = applyLayout(nodes, {})
    const b = applyLayout(shuffled, {})
    for (const node of nodes) {
      expect(b.get(String(node.id))).toEqual(a.get(String(node.id)))
    }
  })

  it('自动布局的节点互不重叠', () => {
    const nodes = makeGraph(60).nodes
    const positions = applyLayout(nodes, {})
    const seen = new Set()
    for (const node of nodes) {
      const { x, y } = positions.get(String(node.id))
      const key = `${x}/${y}`
      expect(seen.has(key)).toBe(false)
      seen.add(key)
    }
  })

  /** 矩形相交判定 —— 保存位置不保证与网格对齐，坐标不等 ≠ 不重叠。 */
  function overlaps(a, b) {
    return a.x < b.x + NODE_WIDTH && a.x + NODE_WIDTH > b.x
      && a.y < b.y + NODE_HEIGHT && a.y + NODE_HEIGHT > b.y
  }

  it('网格避让已保存的位置：混合布局下任意两卡矩形不相交', () => {
    // 两个保存位置各钉死一片区域：
    //   '1' 恰好压在第一个网格单元 (60,60) 上 —— 旧版从不避让，后续节点会盖上来；
    //   '2' 是非网格坐标，且压在单元 (60,220) 上 —— 坐标对不上也必须避让。
    const nodes = makeGraph(40).nodes
    const saved = { 1: { x: 60, y: 60 }, 2: { x: 100, y: 250 } }
    const positions = applyLayout(nodes, saved)
    expect(positions.get('1')).toEqual({ x: 60, y: 60 })      // 拖过的永不动
    expect(positions.get('2')).toEqual({ x: 100, y: 250 })
    // 旧版的"尾部挪一格"在此处也断言不到：拖过的节点不参与网格，网格从第一个
    // 空闲单元继续铺，任何一对矩形都不许相交。
    const rects = nodes.map((n) => ({ ...positions.get(String(n.id)) }))
    for (let i = 0; i < rects.length; i += 1) {
      for (let j = i + 1; j < rects.length; j += 1) {
        expect(overlaps(rects[i], rects[j])).toBe(false)
      }
    }
  })

  it('避让不破坏确定性：同一份 saved 两次跑出同一套位置', () => {
    const nodes = makeGraph(30).nodes
    const saved = { 1: { x: 60, y: 60 } }
    const a = applyLayout(nodes, saved)
    const b = applyLayout([...nodes].reverse(), saved)
    for (const node of nodes) {
      expect(b.get(String(node.id))).toEqual(a.get(String(node.id)))
    }
  })
})

describe('只读：输出集合永远是输入集合的子集', () => {
  it('筛选只会变少，不会变多', () => {
    const graph = makeGraph(50)
    const ids = new Set(graph.nodes.map((n) => String(n.id)))
    const filtered = filterGraph(graph.nodes, graph.edges, { kinds: ['domain'] })
    expect(filtered.nodes.length).toBeLessThanOrEqual(graph.nodes.length)
    for (const node of filtered.nodes) expect(ids.has(String(node.id))).toBe(true)
    for (const edge of filtered.edges) {
      expect(ids.has(String(edge.src))).toBe(true)
      expect(ids.has(String(edge.dst))).toBe(true)
    }
  })

  it('关键词筛选按子串匹配 key', () => {
    const graph = makeGraph(30)
    const filtered = filterGraph(graph.nodes, graph.edges, { keyword: 'host-1.' })
    expect(filtered.nodes.length).toBeGreaterThan(0)
    for (const node of filtered.nodes) expect(node.entity_key).toContain('host-1.')
  })

  it('聚焦返回的是邻域，且一定包含自己', () => {
    const graph = makeGraph(10)
    const within = neighbourhood(graph.nodes, graph.edges, 5, 1)
    expect(within.has('5')).toBe(true)
    expect(within.has('4')).toBe(true)
    expect(within.has('6')).toBe(true)
    expect(within.has('9')).toBe(false)      // 一跳之外
  })

  it('toFlow 不产生任何输入里没有的 id', () => {
    const graph = makeGraph(20)
    const flow = toFlow(graph)
    const ids = new Set(graph.nodes.map((n) => String(n.id)))
    for (const node of flow.nodes) expect(ids.has(node.id)).toBe(true)
    for (const edge of flow.edges) {
      expect(ids.has(edge.source)).toBe(true)
      expect(ids.has(edge.target)).toBe(true)
    }
  })

  it('度数只统计已有的边', () => {
    const graph = makeGraph(4)
    const degree = degreeOf(graph.nodes, graph.edges)
    expect([...degree.values()].reduce((a, b) => a + b, 0)).toBe(graph.edges.length * 2)
  })
})

describe('配色照计划书 §7.3', () => {
  it('kind 色带与文档逐字一致', () => {
    expect(kindColor('domain')).toBe('#4F8DF7')
    expect(kindColor('ip')).toBe('#22B8CF')
    expect(kindColor('service')).toBe('#37B24D')
    expect(kindColor('product')).toBe('#9775FA')
    expect(kindColor('vuln')).toBe('#F03E3E')
  })

  it('未登记的 kind 落到中性色而不是 undefined', () => {
    expect(kindColor('something-new')).toMatch(/^#/)
  })

  it('连线默认色是 §7.3 的 #3A3A45', () => {
    expect(relationColor('unknown_verb')).toBe('#3A3A45')
    expect(relationColor('resolves_to')).toBe('#22B8CF')
  })
})
