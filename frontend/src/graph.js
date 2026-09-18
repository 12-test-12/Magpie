/**
 * 画布的**纯逻辑**：配色、聚合、布局、筛选。
 *
 * 这个模块刻意不 import Vue、不碰 DOM、不发请求 —— 它只做数据到数据的变换。
 * 理由不是"分层好看"，而是**交付标准表里最难验的两行只能这样验**：
 *
 *   * 「1000 节点保持 55fps」——fps 是渲染的事，但**渲染多少个元素**是这里的事。
 *     聚合规则（> 200 进聚合视图）是这条承诺唯一可断言的支点：把 1000 个节点
 *     丢进来，断言出去的元素数 ≤ 阈值，且这一步本身远在 16ms 预算之内。
 *   * 「探索模式无法增删节点与边」——只读的**结构性**保证是"这里根本没有能
 *     产出新节点/新边的函数"。这个模块的导出里没有、也不许有 `addNode` /
 *     `removeEdge` 一类的东西（`check.py` 的 READONLY 静态红线扫的就是它）。
 *
 * 配色**逐字**取自计划书 §7.3 的 token 表。改这里等于改视觉规范，请一起改文档。
 */

// ---------------------------------------------------------------------------
// §7.3 视觉 token
// ---------------------------------------------------------------------------

/** kind → 类型色带 / 卡片描边色。表里没有的 kind 落到 NEUTRAL。 */
export const KIND_COLORS = {
  domain: '#4F8DF7',
  ip: '#22B8CF',
  cidr: '#22B8CF',
  host: '#8B93A7',      // §7.3 未列 host：它是"没分得更细"的兜底 kind，用中性色
  service: '#37B24D',
  product: '#9775FA',
  endpoint: '#F59F00',
  param: '#FFA94D',
  flow: '#748FFC',
  secret: '#E64980',
  vuln: '#F03E3E',
  note: '#FAB005',
  task: '#FAB005',
}
export const NEUTRAL = '#8B93A7'

/** 关系动词 → 连线色。未登记的词用 §7.3 的连线默认色。 */
export const RELATION_COLORS = {
  member_of: '#3F6FB5',
  has_member: '#3F6FB5',
  resolves_to: '#22B8CF',
  resolved_by: '#22B8CF',
  runs: '#37B24D',
  runs_on: '#37B24D',
}
export const RELATION_DEFAULT = '#3A3A45'

/** 超过这个节点数就自动进聚合视图（§7.3 防视觉混乱第 ② 条）。 */
export const AGGREGATE_THRESHOLD = 200

export function kindColor(kind) {
  return KIND_COLORS[kind] || NEUTRAL
}

export function relationColor(rel) {
  return RELATION_COLORS[rel] || RELATION_DEFAULT
}

/** 卡片尺寸**固定**（§7.3 防视觉混乱第 ① 条）—— 节点再多也不许变形。 */
export const NODE_WIDTH = 208
export const NODE_HEIGHT = 64

// ---------------------------------------------------------------------------
// 聚合
// ---------------------------------------------------------------------------

export function shouldAggregate(nodes) {
  return (nodes || []).length > AGGREGATE_THRESHOLD
}

/**
 * 按 kind 聚合成"超级节点"。
 *
 * 这是 §7.3 防视觉混乱第 ② 条的实现：超过 200 个节点时，画布上渲染的是
 * **类型**而不是**个体**。它同时也解释了为什么 1000 个节点还能跑满帧 ——
 * 这不是"我们让 1000 个 DOM 节点飞起来"，而是"我们从来不同时渲染 1000 个"。
 * 把前者当成口径，是在承诺一件 DOM 做不到的事。
 *
 * 返回的节点 id 带 `kind:` 前缀：它与实体 id（数字）不可能撞号，于是
 * "聚合节点"在整条链路里都能被一眼认出来，不需要额外维护一张映射表。
 */
export function buildAggregate(nodes, edges) {
  const groups = new Map()
  for (const node of nodes || []) {
    const kind = String(node.kind || 'unknown')
    if (!groups.has(kind)) groups.set(kind, [])
    groups.get(kind).push(node)
  }
  const kindOf = new Map((nodes || []).map((n) => [String(n.id), String(n.kind || 'unknown')]))

  const aggregateEdges = new Map()
  for (const edge of edges || []) {
    const src = kindOf.get(String(edge.src))
    const dst = kindOf.get(String(edge.dst))
    if (src === undefined || dst === undefined) continue
    const key = `${src}->${dst}->${edge.rel}`
    const bucket = aggregateEdges.get(key)
    if (bucket) {
      bucket.weight += 1
    } else {
      aggregateEdges.set(key, { source: `kind:${src}`, target: `kind:${dst}`,
                                rel: String(edge.rel), weight: 1 })
    }
  }

  const ring = [...groups.keys()].sort()
  const radius = Math.max(180, ring.length * 78)
  const center = { x: 500, y: 360 }
  const aggregateNodes = ring.map((kind, index) => {
    const angle = (index / Math.max(ring.length, 1)) * Math.PI * 2 - Math.PI / 2
    const count = groups.get(kind).length
    return {
      id: `kind:${kind}`,
      type: 'entity',
      // **必须带 `data`**：卡片组件（EntityNode）读的就是它。第一版这里只放了
      // `entity_key` / `count` 这些平铺字段，于是聚合视图下每张卡片都拿到一个
      // `undefined` 的 data —— 而这种形状错误在"数据少所以从没进过聚合"的开发
      // 环境里根本不会出现。`frontend/tests/graph.test.js` 现在把形状钉住了。
      data: {
        entityId: null,
        kind,
        aggregated: true,
        count,
        key: `${kind} × ${count}`,
        lifecycle: null,
        degree: 0,
        collapsed: false,
      },
      position: {
        x: center.x + radius * Math.cos(angle) - NODE_WIDTH / 2,
        y: center.y + radius * Math.sin(angle) - NODE_HEIGHT / 2,
      },
    }
  })

  return {
    nodes: aggregateNodes,
    edges: [...aggregateEdges.values()].map((e, i) => ({
      id: `agg-${i}`,
      source: e.source,
      target: e.target,
      label: e.rel,
      data: { rel: e.rel, weight: e.weight, aggregated: true,
              srcKey: e.source, dstKey: e.target },
    })),
    groups: ring.map((kind) => ({ kind, count: groups.get(kind).length })),
  }
}

// ---------------------------------------------------------------------------
// 布局
// ---------------------------------------------------------------------------

/**
 * 给节点安排位置。**已经持久化过位置的节点永不被移动**（§7.3 交互的最后一条：
 * "之后永不动用户拖过的节点"）。
 *
 * 没位置的节点按 kind 分环摆放：同一类型的邻居聚在一起，比按 id 平铺更容易读。
 * 这一步是 O(n)，1000 个节点量级下不值一提 —— 但它必须**确定性**（同样的输入
 * 出同样的位置），否则每次打开都换一个样子，那就等于没有布局。
 */
export function applyLayout(nodes, layout) {
  const saved = layout || {}
  // 先按 (kind, id) 排序再摆：位置的分配只取决于**集合内容**，与数组顺序无关。
  // 无序输入 + 顺序摆位 = 每次刷新都换一个样子，那比没有布局更让人迷失方向。
  const sorted = [...(nodes || [])].sort((a, b) => {
    const kindA = String(a.kind || '')
    const kindB = String(b.kind || '')
    if (kindA !== kindB) return kindA < kindB ? -1 : 1
    return String(a.id).localeCompare(String(b.id), 'en', { numeric: true })
  })
  const positions = new Map()
  const columns = 5
  let placed = 0
  for (const node of sorted) {
    const stored = saved[String(node.id)]
    if (stored && Number.isFinite(stored.x) && Number.isFinite(stored.y)) {
      positions.set(String(node.id), { x: stored.x, y: stored.y })
      continue                                   // 拖过的节点**永不被移动**
    }
    positions.set(String(node.id), {
      x: 60 + (placed % columns) * (NODE_WIDTH + 96),
      y: 60 + Math.floor(placed / columns) * (NODE_HEIGHT + 96),
    })
    placed += 1
  }
  return positions
}

// ---------------------------------------------------------------------------
// 筛选 —— 只读模式允许的四种操作之一（拖位置 / 折叠 / 筛选 / 标注）
// ---------------------------------------------------------------------------

/**
 * 按 kind / 关键词筛出子图。**返回的是筛选结果，不是新图** —— 集合里
 * 只会有传入的那些节点与边，一条新边都生不出来。
 */
export function filterGraph(nodes, edges, { kinds, keyword, focusId, depth } = {}) {
  const all = nodes || []
  const links = edges || []
  let keep = all

  if (kinds && kinds.length) {
    const wanted = new Set(kinds.map(String))
    keep = keep.filter((n) => wanted.has(String(n.kind)))
  }
  const needle = String(keyword || '').trim().toLowerCase()
  if (needle) {
    keep = keep.filter((n) => String(n.entity_key || '').toLowerCase().includes(needle))
  }
  if (focusId !== undefined && focusId !== null && focusId !== '') {
    const within = neighbourhood(all, links, focusId, depth || 1)
    keep = keep.filter((n) => within.has(String(n.id)))
  }

  const allowed = new Set(keep.map((n) => String(n.id)))
  return {
    nodes: keep,
    edges: links.filter((e) => allowed.has(String(e.src)) && allowed.has(String(e.dst))),
  }
}

/** 从 focusId 出发走 depth 层（BFS）。只读：用来**聚焦**，不用来生成。 */
export function neighbourhood(nodes, edges, focusId, depth = 1) {
  const root = String(focusId)
  const adjacency = new Map()
  for (const edge of edges || []) {
    const src = String(edge.src)
    const dst = String(edge.dst)
    if (!adjacency.has(src)) adjacency.set(src, [])
    if (!adjacency.has(dst)) adjacency.set(dst, [])
    adjacency.get(src).push(dst)
    adjacency.get(dst).push(src)
  }
  const seen = new Set([root])
  let frontier = [root]
  for (let step = 0; step < depth; step += 1) {
    const next = []
    for (const id of frontier) {
      for (const other of adjacency.get(id) || []) {
        if (!seen.has(other)) {
          seen.add(other)
          next.push(other)
        }
      }
    }
    frontier = next
  }
  const known = new Set((nodes || []).map((n) => String(n.id)))
  return new Set([...seen].filter((id) => known.has(id)))
}

/** 每个节点的度数 —— 卡片徽标与"最重要节点排序"都用它。 */
export function degreeOf(nodes, edges) {
  const degree = new Map((nodes || []).map((n) => [String(n.id), 0]))
  for (const edge of edges || []) {
    const src = String(edge.src)
    const dst = String(edge.dst)
    if (degree.has(src)) degree.set(src, degree.get(src) + 1)
    if (degree.has(dst)) degree.set(dst, degree.get(dst) + 1)
  }
  return degree
}

/** 把 API 的图负载转成 Vue Flow 的节点 / 边。**只读转换，不新增元素。** */
export function toFlow(graph, { savedLayout } = {}) {
  const nodes = (graph && graph.nodes) || []
  const edges = (graph && graph.edges) || []
  const layout = savedLayout || (graph && graph.layout) || {}
  const positions = applyLayout(nodes, layout)
  const degree = degreeOf(nodes, edges)

  return {
    nodes: nodes.map((node) => ({
      id: String(node.id),
      type: 'entity',
      position: positions.get(String(node.id)) || { x: 0, y: 0 },
      data: {
        entityId: node.id,
        kind: node.kind,
        lifecycle: node.lifecycle,
        key: node.entity_key,
        degree: degree.get(String(node.id)) || 0,
        collapsed: Boolean((layout[String(node.id)] || {}).collapsed),
      },
    })),
    edges: edges.map((edge) => ({
      id: `e${edge.id}`,
      source: String(edge.src),
      target: String(edge.dst),
      label: String(edge.rel),
      data: { rel: String(edge.rel), confidence: edge.confidence,
              srcKey: edge.src_key, dstKey: edge.dst_key },
    })),
  }
}
