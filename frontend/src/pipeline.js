/**
 * 编排模式的**纯逻辑**：定义 ↔ 画布、依赖图、增删步骤与连线。
 *
 * 与 `graph.js` 同一条纪律：不 import Vue、不碰 DOM、不发请求 —— 它只做数据到
 * 数据的变换。理由不是"分层好看"，而是**"可编辑"这件事必须能被断言**：
 *
 *   * 「画布是受控的」——受控的全部含义就是"图上有什么"完全由 `definition`
 *     这一个值算出来。所以 `definitionToFlow(definition)` 是一个**纯函数**，
 *     同样的定义永远算出同样的节点与边。
 *   * 「连线只表达依赖（边）」——`withConnection` 只往 `after` 里加一个步骤 id，
 *     它生不出第二种东西来。3.7a 冻结 F7 说"一个手势换一个洞"，那个洞能开的
 *     前提正是这里所有的手势都只改这一个字段。
 *
 * ⚠️ 这个模块的导出里**没有**、也**不许有**任何 Vue Flow 的可变 API 名字
 * （`check.py` 的 READONLY 静态红线扫 `frontend/src/**`）。图的状态不属于画布
 * 组件，也不属于这里 —— 它属于定义。
 */

/** `source.kind` 的三个取值。与 `magpie/core/pipeline.py` 的 `SOURCE_KINDS`
 *  逐字一致；界面据此渲染下拉，而**能跑什么**由数据层说了算（这里写错一个
 *  字母的后果是选完就报 400，不是静默跑错）。 */
export const SOURCE_KINDS = [
  { value: 'glob', label: 'glob（通配，0..N 个文件）' },
  { value: 'file', label: 'file（指名道姓的单个文件）' },
  { value: 'dir', label: 'dir（目录，递归发现）' },
]

/** `authority` 的四个取值（`schema/001_init.sql` 的 CHECK，唯一事实来源）。 */
export const AUTHORITY_LEVELS = ['unverified', 'semiautomatic', 'manual', 'verified']

/** 一条**空定义**。`format_version` 跟着数据层的当前版本走 —— 界面不发明版本。 */
export function emptyDefinition(project) {
  return {
    format_version: 1,
    name: '新管线',
    project: project || null,
    base_dir: null,
    data_dir: null,
    scope: null,
    steps: [],
  }
}

/**
 * 稳定序列化 —— `dirty` 判据的底座。
 *
 * `dirty` 是**一次深比较**（冻结 F8），不用手维护的布尔位：那种位会在某个分支里
 * 忘记翻转，然后"未保存"标记开始说谎。深比较的前提是"同一个值的两种写法得到
 * 同一个字符串"，所以 key 要排序 —— `{a:1,b:2}` 与 `{b:2,a:1}` 是同一份定义。
 */
export function canonical(value) {
  return JSON.stringify(sortDeep(value === undefined ? null : value))
}

function sortDeep(value) {
  if (Array.isArray(value)) return value.map(sortDeep)
  if (value && typeof value === 'object') {
    const out = {}
    for (const key of Object.keys(value).sort()) out[key] = sortDeep(value[key])
    return out
  }
  return value
}

export function stepsOf(definition) {
  return (definition && definition.steps) || []
}

export function stepById(definition, id) {
  return stepsOf(definition).find((step) => String(step.id) === String(id)) || null
}

/** 依赖边：`after` 里的每个引用画一条 `dep → step`。**图的内容完全来自定义。** */
export function dependencyEdges(definition) {
  const ids = new Set(stepsOf(definition).map((step) => String(step.id)))
  const edges = []
  for (const step of stepsOf(definition)) {
    for (const dep of step.after || []) {
      if (ids.has(String(dep))) {
        edges.push({ source: String(dep), target: String(step.id) })
      }
    }
  }
  return edges
}

/** 从 `from` 出发能不能走到 `to`。连线前用它判环 —— 环是数据层会拒绝的定义。 */
export function reaches(definition, from, to) {
  const adjacency = new Map()
  for (const edge of dependencyEdges(definition)) {
    if (!adjacency.has(edge.source)) adjacency.set(edge.source, [])
    adjacency.get(edge.source).push(edge.target)
  }
  const seen = new Set([String(from)])
  const stack = [String(from)]
  while (stack.length) {
    const node = stack.pop()
    if (node === String(to)) return true
    for (const next of adjacency.get(node) || []) {
      if (!seen.has(next)) {
        seen.add(next)
        stack.push(next)
      }
    }
  }
  return false
}

/** 连一条边（`source` 先跑，`target` 后跑）能成立吗？ */
export function canConnect(definition, source, target) {
  const src = String(source)
  const dst = String(target)
  if (src === dst) return { ok: false, why: '一个步骤不能依赖它自己' }
  if (!stepById(definition, src) || !stepById(definition, dst)) {
    return { ok: false, why: '连线的两端必须都是这条定义里的步骤' }
  }
  const existing = dependencyEdges(definition)
    .some((edge) => edge.source === src && edge.target === dst)
  if (existing) return { ok: false, why: `${dst} 已经依赖 ${src} 了` }
  if (reaches(definition, dst, src)) {
    // 连上去就成环了。数据层（`pipeline.order_steps`）会拒绝成环的定义，
    // 但那时已经存进库了 —— 在这里挡住，用户看到的是"连不上"，不是一次 400。
    return { ok: false, why: `会成环：${dst} 已经（间接）先于 ${src}` }
  }
  return { ok: true, why: '' }
}

// ---------------------------------------------------------------------------
// 定义的**纯变换**：每个函数返回一份新定义，不就地改
// ---------------------------------------------------------------------------

function withSteps(definition, steps) {
  return Object.assign({}, definition, { steps })
}

/** 把 `patch` 并进一个步骤。`id` 是身份，**不允许改**（改了等于换了个步骤）。 */
export function withStepPatch(definition, id, patch) {
  const next = Object.assign({}, patch)
  delete next.id
  return withSteps(definition, stepsOf(definition).map((step) => (
    String(step.id) === String(id) ? Object.assign({}, step, next) : step)))
}

/** 改依赖集合（`after` 只增删 id，不产生别的东西）。 */
export function withDependencies(definition, id, after) {
  return withStepPatch(definition, id, { after: [...new Set(after.map(String))].sort() })
}

export function withConnection(definition, source, target) {
  const check = canConnect(definition, source, target)
  if (!check.ok) return definition
  const step = stepById(definition, target)
  return withDependencies(definition, target, [...(step.after || []), String(source)])
}

export function withDisconnection(definition, source, target) {
  const step = stepById(definition, target)
  if (!step) return definition
  return withDependencies(definition, target,
    (step.after || []).filter((dep) => String(dep) !== String(source)))
}

/** 生成一个没被占用的步骤 id（人读的顺序号，不是 uuid —— 这是自用工具）。 */
export function nextStepId(definition) {
  const taken = new Set(stepsOf(definition).map((step) => String(step.id)))
  for (let index = 1; index < 1000; index += 1) {
    const candidate = `step${index}`
    if (!taken.has(candidate)) return candidate
  }
  return 'step?'
}

export function withAddedStep(definition) {
  const id = nextStepId(definition)
  const step = {
    id,
    source: { kind: 'glob', pattern: '' },
    source_name: null,
    parser: null,
    after: [],
    authority: 'unverified',
    archive: 'archived',
    batch_size: 2000,
    dry_run: false,
  }
  return withSteps(definition, [...stepsOf(definition), step])
}

/**
 * 删一个步骤，并**顺带清掉别人对它的引用**。
 *
 * 不清的话，库里会留下一份 `after` 指向不存在步骤的定义 —— 数据层的
 * `normalize_definition` 会拒绝它，于是"删一个步骤"变成"这条定义再也存不下去"，
 * 而报错指向的是一个用户没写过的字段。
 */
export function withRemovedStep(definition, id) {
  const key = String(id)
  const steps = stepsOf(definition)
    .filter((step) => String(step.id) !== key)
    .map((step) => Object.assign({}, step, {
      after: (step.after || []).filter((dep) => String(dep) !== key),
    }))
  return withSteps(definition, steps)
}

// ---------------------------------------------------------------------------
// 定义 → 画布
// ---------------------------------------------------------------------------

/** 步骤卡片的尺寸（与实体卡片同宽，两个模式的画布看起来才像同一个工具）。 */
export const STEP_WIDTH = 208
export const STEP_HEIGHT = 72
const COLUMN_GAP = 96
const ROW_GAP = 88

/**
 * 定义 → Vue Flow 的节点与边。**纯函数**，同样的定义永远算出同样的图。
 *
 * 布局是**确定性网格**（与 `graph.js` 的 `applyLayout` 同一条理由：图每次打开
 * 都长得不一样比排得不好看糟得多）。§7.3 提到的 dagre / elk 是**探索模式**的
 * 自动布局，本步不引新依赖。
 */
export function definitionToFlow(definition, { selectedId } = {}) {
  const steps = stepsOf(definition)
  const order = topoOrder(definition)
  const rank = new Map(order.map((id, index) => [id, index]))
  const buckets = new Map()
  for (const step of steps) {
    const level = rank.has(String(step.id)) ? rank.get(String(step.id)) : steps.length
    const column = Math.floor(level / 4)
    const row = level % 4
    if (!buckets.has(step.id)) {
      buckets.set(String(step.id), {
        x: 60 + column * (STEP_WIDTH + COLUMN_GAP),
        y: 60 + row * (STEP_HEIGHT + ROW_GAP),
      })
    }
  }
  const nodes = steps.map((step) => ({
    id: String(step.id),
    type: 'step',
    position: buckets.get(String(step.id)) || { x: 60, y: 60 },
    data: {
      stepId: String(step.id),
      label: String(step.id),
      parser: step.parser || null,
      sourceKind: (step.source || {}).kind || 'glob',
      pattern: (step.source || {}).pattern || '',
      sourceName: step.source_name || null,
      dependsOn: (step.after || []).map(String),
      dryRun: Boolean(step.dry_run),
      selected: selectedId !== undefined && String(selectedId) === String(step.id),
    },
  }))
  const edges = dependencyEdges(definition).map((edge, index) => ({
    id: `dep-${edge.source}-${edge.target}-${index}`,
    source: edge.source,
    target: edge.target,
    label: 'after',
    animated: false,
  }))
  return { nodes, edges }
}

/**
 * 稳定的拓扑序（Kahn + 按 id 字典序取下一个）。
 *
 * 自己写一份而不是复用数据层的 `order_steps`：那是 Python。**判据是两边对同一
 * 份定义给出同一个顺序**，而两条实现分叉的症状只是"界面上的排布换了" —— 所以
 * 这里只用来**摆位置**，不参与任何判定。
 */
export function topoOrder(definition) {
  const steps = stepsOf(definition)
  const ids = steps.map((step) => String(step.id)).sort()
  const deps = new Map(steps.map((step) => [
    String(step.id), (step.after || []).map(String).filter((d) => ids.includes(d))]))
  const done = []
  const seen = new Set()
  while (done.length < ids.length) {
    const ready = ids.filter((id) => !seen.has(id)
      && deps.get(id).every((dep) => seen.has(dep)))
    if (!ready.length) break                       // 有环：把剩下的原样接在后面
    seen.add(ready[0])
    done.push(ready[0])
  }
  for (const id of ids) if (!seen.has(id)) done.push(id)
  return done
}
