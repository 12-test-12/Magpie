/**
 * 应用的共享状态。**只读画布的全部保证都落在这个文件上。**
 *
 * ⚠️ 这个模块的导出里**没有**、也**不许有**任何"往图里加东西"的能力：
 * 没有 `addNode` / `addEdge` / `removeNode` / `removeEdge` / `setNodes` /
 * `setEdges` / `applyNodeChanges`，也没有 `onConnect` 的连接回调。
 *
 * 这不是"我们记得不要调用"式的自觉 —— `check.py` 的 READONLY 静态红线会扫
 * `frontend/src/**`，出现这些名字就让整轮验证变红；`frontend/tests/readonly.test.js`
 * 还会真的挂载一次画布，断言连线的入口是关死的。
 *
 * 探索模式允许的四件事（计划书 §7.2）在这里分别对应：
 *
 *     拖位置  → `moveEntity`（改的是本地坐标 + `entity_layout` 持久化）
 *     折叠    → `toggleCollapse`
 *     筛选    → `setFilter` / `toggleKind` / `clearFilter`
 *     标注    → `tagEntity`（`PATCH /entities/{id}` 的 add_tags，2.7 就有的端点）
 *
 * 图的**内容**只能来自 `GET /graph`。画布上出现过什么，库里就有什么 ——
 * 这是"探索模式"与"编排模式"在数据流上的分界，不只是两个不同的按钮样式。
 *
 * 形态说明：整个 store 是**一个 `reactive` 对象**，内部全用 `ref` / `computed`。
 * `reactive` 会把 ref 解包，于是消费方（组件）统一写 `store.counts.entities`，
 * 不必记住哪个字段要 `.value`。一个"有时要 .value、有时不要"的状态模块，
 * 迟早会在某个组件里写成 `store.visible.nodes` 然后静默拿到 undefined。
 */

import { computed, reactive, ref, shallowRef } from 'vue'
import { get, patch, post, put, watchRun } from './api.js'
import { buildAggregate, filterGraph, shouldAggregate, toFlow } from './graph.js'
import {
  canonical, definitionToFlow, emptyDefinition, withAddedStep, withConnection,
  withDisconnection, withRemovedStep, withStepPatch,
} from './pipeline.js'

// ---------------------------------------------------------------------------
// 内部状态
// ---------------------------------------------------------------------------

const _projects = ref([])
const _project = ref('')
const _meta = ref(null)
//: 镜头载荷，键是镜头 id。**空对象起步**：有哪几个镜头由库里的
//: `kind_registry.lens` 决定（`/meta` 把它带回来），不在这里写死。
const _lenses = reactive({})
const _lensKeys = ref([])
const _openInboxCount = ref(0)

const _raw = reactive({ nodes: [], edges: [], layout: {} })
const _loading = ref(false)
const _loadError = ref('')
const _selectedId = ref(null)
const _selectedDetail = ref(null)
const _selectedLoading = ref(false)
const _filter = reactive({ kinds: [], keyword: '', focusId: null, depth: 1 })
const _logLines = shallowRef([])
const _view = ref('explore')
const _paletteOpen = ref(false)
const _drawerOpen = ref(true)
const _activeImportId = ref(null)
const _importProgress = reactive({ state: '', records: 0, line: 0, badLines: 0,
                                   stage: '', dropped: 0 })

// ---------------------------------------------------------------------------
// 编排模式的草稿（`view === 'pipeline'`）
// ---------------------------------------------------------------------------
//
// **定义是唯一的事实来源，画布不持有自己的图状态**（3.7a 冻结 F6）。
// `_pipelineFlow` 是 `definition` 的一次 `computed`，与探索模式的
// `GraphCanvas.vue` 拿 `store.visible` 是同构的。
//
// 让画布组件自己持图状态会引入**第二个事实来源**，而两份状态之间同步的 bug
// 没有任何症状，直到有人保存了一份少了一块的管线。
const _pipeline = reactive({
  id: null,             // 当前打开的定义 id；null = 还没存过的新草稿
  revision: null,       // 乐观锁。保存时原样带回，不匹配就是 409
  list: [],             // 左栏的管线列表
  definition: null,     // 草稿（唯一事实来源）
  saved: null,          // 上一次保存/载入的那一份 —— `dirty` 拿它来比
  flow: { nodes: [], edges: [] },
  selectedStepId: null,
  runs: [],             // 这条定义的运行历史
  activeRunId: null,
  //: 真跑的进度。**全部来自 SSE**（也就是数据层的 `on_progress`）——
  //: 界面自己记的账迟早会出现"进度条说 80%，报告说 100%"。
  progress: { records: 0, line: 0, file: '', done: 0, total: 0 },
  loading: false,
  saving: false,
  error: '',
})

/** 一次**深比较**（冻结 F8）。不用手维护的布尔位 —— 那种位会在某个分支里忘记
 *  翻转，然后"未保存"标记开始说谎。 */
const _pipelineDirty = computed(() => (
  _pipeline.definition !== null
  && canonical(_pipeline.definition) !== canonical(_pipeline.saved)))

const LOG_CAP = 500

/** 日志抽屉最多留多少行。它不是审计日志（那是 `activity_log`）—— 无上限地
 *  攒下去，一次十万行的导入就能把这个页面拖死。 */
async function _projectQuery() {
  return _project.value ? `project=${encodeURIComponent(_project.value)}` : ''
}

// ---------------------------------------------------------------------------
// 派生
// ---------------------------------------------------------------------------

const _counts = computed(() => ({
  entities: _raw.nodes.length,
  edges: _raw.edges.length,
  rendered: _visible.value.nodes.length,
  aggregated: _visible.value.aggregated,
}))

/** 超过阈值就自动进聚合视图（§7.3 防视觉混乱第 ② 条）。 */
const _aggregate = computed(() => shouldAggregate(_raw.nodes))

/** 左栏要渲染的镜头清单：**有哪些镜头**来自库（`kind_registry.lens`），
 *  这里只负责排个稳定的顺序、配个人读的名字。 */
const _lensRows = computed(() => _lensKeys.value.map((key) => ({
  key,
  label: LENS_LABELS[key] || key,
  data: _lenses[key],
})))

/** 编排画布该渲染什么：**完全由草稿算出来**（见上面 F6 那段）。
 *  定义是空的时候给一张空图，而不是 `null` —— 组件的 props 契约里没有"可能
 *  是 null"这一条，少一个分支就少一处只在空库上才炸的地方。 */
const _pipelineFlow = computed(() => (
  _pipeline.definition
    ? definitionToFlow(_pipeline.definition, { selectedId: _pipeline.selectedStepId })
    : { nodes: [], edges: [] }))

/** 当前该渲染什么：聚合后的类型节点，还是逐个实体。 */
const _visible = computed(() => {
  const filtered = filterGraph(_raw.nodes, _raw.edges, {
    kinds: _filter.kinds, keyword: _filter.keyword,
    focusId: _filter.focusId, depth: _filter.depth,
  })
  if (shouldAggregate(filtered.nodes)) {
    const grouped = buildAggregate(filtered.nodes, filtered.edges)
    return { ...grouped, aggregated: true, entityCount: filtered.nodes.length }
  }
  const flow = toFlow({ nodes: filtered.nodes, edges: filtered.edges,
                        layout: _raw.layout })
  return { nodes: flow.nodes, edges: flow.edges, aggregated: false,
           entityCount: filtered.nodes.length, groups: null }
})

// ---------------------------------------------------------------------------
// 导出
// ---------------------------------------------------------------------------

export const VIEWS = [
  { key: 'explore', label: '画布', hint: '探索模式（只读）' },
  // 编排模式只是 `store.view` 的**一个新取值**（冻结 F5）—— 不新增第二套路由
  // 状态。第二套路由状态的表现是"刷新之后回到另一个页面"，而那在本地工具里
  // 会被当成"偶发"。
  { key: 'pipeline', label: '编排', hint: '管线编排（可编辑）' },
  { key: 'assets', label: '资产', hint: '镜头：域名 / IP / 主机 / 网段' },
  { key: 'services', label: '服务与中间件', hint: '镜头：服务 / 产品' },
  { key: 'vuln', label: '漏洞', hint: '镜头：漏洞（nuclei 的命中）' },
  { key: 'jobs', label: '作业历史', hint: '导入 / undo / redo' },
  { key: 'inbox', label: 'Inbox', hint: '待裁决' },
  { key: 'doctor', label: '数据体检', hint: '完整性 / 孤儿 / 未测' },
]

//: 镜头的人读名字与**展示顺序**（计划书 §3.5 的六个）。两者都是**界面**的事：
//: "有哪几个镜头"由 `kind_registry.lens` 说了算，`/meta` 把它带回来。这里排不
//: 进来的镜头（以后新增的）不会消失 —— 它们排在已知的之后，名字就用 id。
const LENS_LABELS = {
  assets: '资产',
  services: '服务与中间件',
  api: '接口',
  traffic: '流量',
  vuln: '漏洞',
  graph: '图谱',
}
const LENS_ORDER = ['assets', 'services', 'api', 'traffic', 'vuln', 'graph']

export const store = reactive({
  // -- 状态 --
  // `VIEWS` 也放进来：外壳按它渲染页签。放在这里而不是让每个组件各自 import，
  // 是为了让"有哪几页"是**一份**清单 —— 两份清单迟早会分叉，而症状是某个
  // 页面在导航里静默消失。
  VIEWS,
  projects: _projects,
  project: _project,
  meta: _meta,
  lenses: _lenses,
  openInboxCount: _openInboxCount,
  loading: _loading,
  loadError: _loadError,
  selectedId: _selectedId,
  selectedDetail: _selectedDetail,
  selectedLoading: _selectedLoading,
  filter: _filter,
  logLines: _logLines,
  view: _view,
  paletteOpen: _paletteOpen,
  drawerOpen: _drawerOpen,
  activeImportId: _activeImportId,
  importProgress: _importProgress,
  pipeline: _pipeline,

  // -- 派生 --
  counts: _counts,
  aggregate: _aggregate,
  visible: _visible,
  lensRows: _lensRows,
  lensKeys: _lensKeys,
  pipelineFlow: _pipelineFlow,
  pipelineDirty: _pipelineDirty,

  // -- 动作 --
  pushLog,
  clearLog,
  loadMeta,
  loadProjects,
  loadLenses,
  loadGraph,
  reload,
  selectProject,
  createProject,
  selectEntity,
  moveEntity,
  toggleCollapse,
  persistLayout,
  setFilter,
  toggleKind,
  clearFilter,
  tagEntity,
  submitImport,
  resetImportProgress,
  listJobs,
  jobDetail,
  undoJob,
  redoJob,
  undoImport,
  setView,
  // 编排模式（可编辑只在这里）
  loadPipelines,
  newPipelineDraft,
  selectPipeline,
  selectStep,
  patchDefinition,
  patchStep,
  addStep,
  removeStep,
  connectSteps,
  disconnectSteps,
  savePipeline,
  archivePipeline,
  runPipeline,
  loadPipelineRuns,
  undoRun,
  redoRun,
})

// ---------------------------------------------------------------------------
// 日志
// ---------------------------------------------------------------------------

export function pushLog(text, kind = 'import', level = 'info') {
  const next = _logLines.value.concat([{ at: Date.now(), text, kind, level }])
  _logLines.value = next.length > LOG_CAP ? next.slice(next.length - LOG_CAP) : next
}

export function clearLog() {
  _logLines.value = []
}

// ---------------------------------------------------------------------------
// 项目 / 元信息
// ---------------------------------------------------------------------------

export const INBOX_LIMIT = 100

export async function loadMeta() {
  _meta.value = await get('/meta')
  _openInboxCount.value = (_meta.value.counts || {}).open_inbox || 0
  return _meta.value
}

export async function loadProjects() {
  _projects.value = await get('/projects')
  if (!_project.value && _projects.value.length === 1) {
    _project.value = _projects.value[0].name
  }
  return _projects.value
}

export async function createProject(name) {
  const created = await post('/projects', { name, scope: {} })
  await loadProjects()
  return created
}

/**
 * 取回**库里真有归属**的每一个镜头。
 *
 * 镜头清单来自 `GET /meta` 的 `kinds[].lens` —— 也就是 `kind_registry.lens`，
 * 那份词表的唯一事实来源。**刻意不在这里写死 `['assets', 'services']`**：
 * 写死之后，"3.2 登记了 vuln kind"这件事不会让漏洞镜头出现，而症状是
 * "数据在库里、界面里没有那一页"—— 一种只能靠人记住去改代码的坏法。
 *
 * 反过来，某个镜头没有归属的 kind 时 `GET /lenses/{lens}` 会 404 —— 那也不是
 * 故障：界面显示"这个镜头还是空的"就好。
 */
export async function loadLenses() {
  const meta = _meta.value || await loadMeta()
  const keys = [...new Set((meta.kinds || []).map((kind) => kind.lens))]
    .filter(Boolean)
    .sort((a, b) => {
      const rank = (key) => {
        const index = LENS_ORDER.indexOf(key)
        return index < 0 ? LENS_ORDER.length : index
      }
      return rank(a) - rank(b) || String(a).localeCompare(String(b))
    })
  _lensKeys.value = keys
  for (const lens of keys) {
    try {
      _lenses[lens] = await get(`/lenses/${lens}`)
    } catch (err) {
      _lenses[lens] = { lens, kinds: [], total: 0, missing: true }
    }
  }
}

export async function loadGraph() {
  _loading.value = true
  _loadError.value = ''
  try {
    const query = await _projectQuery()
    const data = await get(`/graph?limit=500${query ? `&${query}` : ''}`)
    _raw.nodes = data.nodes || []
    _raw.edges = data.edges || []
    _raw.layout = data.layout || {}
  } catch (err) {
    _loadError.value = err.message
    _raw.nodes = []
    _raw.edges = []
    _raw.layout = {}
  } finally {
    _loading.value = false
  }
}

export async function reload() {
  // 顺序有意：图形要项目定下来才好读；而**镜头清单现在是 `meta` 推导出来的**
  // （`meta.kinds[].lens`），所以它必须排在 `loadLenses` 前面 —— 两件事原来
  // 是并行的，那是"镜头清单写死在代码里"才成立的前提。代价是首屏多一个 RTT，
  // 换来的是"登记一个 kind，镜头页自己出现"，值得。
  await loadProjects()
  await loadMeta()
  await Promise.all([loadGraph(), loadLenses()])
}

export async function selectProject(name) {
  _project.value = name
  _selectedId.value = null
  _selectedDetail.value = null
  // **换项目 = 丢弃草稿**（冻结 F8）。这是唯一真正危险的情况：草稿里的 `project`
  // 与新的项目不是同一个，把它带过去就是在跨项目写事实 —— 而跨项目污染是这套
  // 东西里唯一不能靠"小心一点"避免的错误。
  _pipeline.id = null
  _pipeline.revision = null
  _pipeline.definition = null
  _pipeline.saved = null
  _pipeline.selectedStepId = null
  _pipeline.list = []
  _pipeline.runs = []
  _pipeline.activeRunId = null
  _pipeline.error = ''
  await reload()
}

// ---------------------------------------------------------------------------
// 选择 / 详情
// ---------------------------------------------------------------------------

export async function selectEntity(id) {
  _selectedId.value = id
  _selectedDetail.value = null
  if (id === null || id === undefined) return null
  if (String(id).startsWith('kind:')) return null      // 聚合节点没有详情
  _selectedLoading.value = true
  try {
    _selectedDetail.value = await get(`/entities/${id}`)
    return _selectedDetail.value
  } catch (err) {
    pushLog(`读取实体 ${id} 失败：${err.message}`, 'import', 'error')
    return null
  } finally {
    _selectedLoading.value = false
  }
}

// ---------------------------------------------------------------------------
// 探索模式允许的四种操作
// ---------------------------------------------------------------------------

/** 拖位置。只改**本地坐标 + 持久化布局**，不动图的构成。 */
export function moveEntity(id, position) {
  if (!position || !Number.isFinite(position.x) || !Number.isFinite(position.y)) return
  _raw.layout[String(id)] = Object.assign({}, _raw.layout[String(id)], {
    x: position.x, y: position.y,
  })
}

/** 折叠。同上：它影响**怎么显示**，不影响**有什么**。 */
export function toggleCollapse(id) {
  const key = String(id)
  const current = _raw.layout[key] || {}
  _raw.layout[key] = { x: current.x ?? 0, y: current.y ?? 0,
                       collapsed: !current.collapsed }
}

const LAYOUT_FLUSH_MS = 400
let layoutTimer = null
let pendingLayout = new Map()

/**
 * 把本地坐标写回 `entity_layout`（§7.3 防视觉混乱第 ③ 条）。
 *
 * **合并 + 去抖**：拖一次节点会来几十个 move 事件，一个一个 PATCH 会把单写
 * 队列塞满 —— 而写队列是导入共用的那一条。位置这种"最后一帧才算数"的数据，
 * 合并是唯一正确的攒批方式。批里带的是 `upsert` 语义的整条记录，所以丢掉
 * 中间帧不会丢数据。
 */
export function persistLayout(id, position, { immediate = false } = {}) {
  moveEntity(id, position)
  pendingLayout.set(String(id), {
    entity_id: Number(id), x: position.x, y: position.y, collapsed: false,
  })
  const flush = async () => {
    const batch = [...pendingLayout.values()]
    pendingLayout = new Map()
    if (!batch.length) return
    try {
      const query = await _projectQuery()
      await patch(`/graph/layout${query ? `?${query}` : ''}`, { items: batch })
    } catch (err) {
      pushLog(`保存布局失败：${err.message}`, 'import', 'warn')
    }
  }
  if (layoutTimer) clearTimeout(layoutTimer)
  if (immediate) {
    flush()
    return
  }
  layoutTimer = setTimeout(flush, LAYOUT_FLUSH_MS)
}

export function setFilter(next) {
  if (next.kinds !== undefined) _filter.kinds = next.kinds
  if (next.keyword !== undefined) _filter.keyword = next.keyword
  if (next.focusId !== undefined) _filter.focusId = next.focusId
  if (next.depth !== undefined) _filter.depth = next.depth
}

export function toggleKind(kind) {
  const set = new Set(_filter.kinds)
  if (set.has(kind)) set.delete(kind)
  else set.add(kind)
  _filter.kinds = [...set]
}

export function clearFilter() {
  _filter.kinds = []
  _filter.keyword = ''
  _filter.focusId = null
  _filter.depth = 1
}

/** 标注：加一个标签。走 2.7 就有的 `PATCH /entities/{id}`（写值那条路仍然 501）。 */
export async function tagEntity(id, tag) {
  const data = await patch(`/entities/${id}`, { add_tags: [tag] })
  if (_selectedDetail.value && _selectedDetail.value.entity.id === id) {
    _selectedDetail.value = Object.assign({}, _selectedDetail.value, { tags: data.tags })
  }
  pushLog(`实体 ${id} 加了标签 ${tag}`, 'import')
  return data
}

// ---------------------------------------------------------------------------
// 作业（导入 / undo / redo）
// ---------------------------------------------------------------------------

export function resetImportProgress() {
  _importProgress.state = ''
  _importProgress.records = 0
  _importProgress.line = 0
  _importProgress.badLines = 0
  _importProgress.stage = ''
  _importProgress.dropped = 0
}

export async function submitImport({ paths, parser, dryRun, archive }) {
  const body = { paths, dry_run: Boolean(dryRun), archive: archive || 'archived' }
  if (parser) body.parser = parser
  if (_project.value) body.project = _project.value
  const record = await post('/imports', body)
  _activeImportId.value = record.import_id
  resetImportProgress()
  _importProgress.state = record.state
  return record
}

// 刻意用**函数声明**而不是 `const fn = () => …`：`export const store = reactive({…})`
// 在模块求值时就引用了它们，而 `const` 此刻还在 TDZ 里（会抛
// "Cannot access 'listJobs' before initialization"）。函数声明会提升，
// 于是"导出面在 store 之前"这件事不会变成一个只在 import 时才炸的坑。
export function listJobs(limit = 100) {
  return get(`/jobs?limit=${limit}`)
}

export function jobDetail(jobId) {
  return get(`/jobs/${jobId}`)
}

export function undoJob(jobId, dryRun) {
  return post(`/jobs/${jobId}/undo`, { dry_run: Boolean(dryRun) })
}

export function redoJob(jobId, dryRun) {
  return post(`/jobs/${jobId}/redo`, { dry_run: Boolean(dryRun) })
}

export function undoImport(importId) {
  return post(`/imports/${importId}/undo`, {})
}

// ---------------------------------------------------------------------------
// 编排模式：存下来的定义 / 草稿 / 跑一次
// ---------------------------------------------------------------------------
//
// 这一节**只发请求，不写库**（写库只经服务端的单写队列，R1）。界面上"可编辑"
// 的全部含义是"改这份草稿"：草稿在内存里，保存时才变成一次 `POST` / `PUT`。
//
// `_draft()` 返回草稿的一份**深拷贝** —— 变换函数都是纯函数（返回新对象），
// 但 `_pipeline.definition` 是 reactive 的，把它的引用交给变换函数再赋回去，
// 中间任何一步就地改都会绕过 Vue 的响应式。

function _copy(value) {
  return value === null || value === undefined ? value : JSON.parse(JSON.stringify(value))
}

function _draft() {
  return _copy(_pipeline.definition)
}

export async function loadPipelines() {
  try {
    const query = await _projectQuery()
    _pipeline.list = await get(`/pipelines${query ? `?${query}` : ''}`)
  } catch (err) {
    _pipeline.error = err.message
    pushLog(`读取管线列表失败：${err.message}`, 'pipeline', 'error')
  }
  return _pipeline.list
}

/** 开一份**没存过**的新草稿。它不落库 —— 直到用户按保存。 */
export function newPipelineDraft() {
  _pipeline.id = null
  _pipeline.revision = null
  _pipeline.definition = emptyDefinition(_project.value)
  _pipeline.saved = null
  _pipeline.selectedStepId = null
  _pipeline.runs = []
  _pipeline.activeRunId = null
  _pipeline.error = ''
  pushLog('新建了一份管线草稿（还没有保存）', 'pipeline')
}

export async function selectPipeline(id) {
  _pipeline.loading = true
  _pipeline.error = ''
  try {
    const query = await _projectQuery()
    const detail = await get(`/pipelines/${id}${query ? `?${query}` : ''}`)
    _pipeline.id = detail.id
    _pipeline.revision = detail.revision
    _pipeline.definition = detail.definition
    // `saved` 是**另一份对象**：`dirty` 是这两份的深比较。让它们指向同一个
    // 对象的话，改草稿会连"saved"一起改，"未保存"就永远不会亮。
    _pipeline.saved = _copy(detail.definition)
    _pipeline.selectedStepId = ((detail.definition || {}).steps || [])
      .map((step) => String(step.id))[0] || null
    _pipeline.runs = []
    await loadPipelineRuns(detail.id)
  } catch (err) {
    _pipeline.error = err.message
    pushLog(`打开管线 ${id} 失败：${err.message}`, 'pipeline', 'error')
  } finally {
    _pipeline.loading = false
  }
}

export function selectStep(id) {
  _pipeline.selectedStepId = id === null || id === undefined ? null : String(id)
}

/** 改定义**级**的字段（名字 / base_dir / data_dir）。步骤上的字段走 `patchStep`。 */
export function patchDefinition(patch) {
  _pipeline.definition = Object.assign({}, _draft(), patch)
}

export function patchStep(id, patch) {
  _pipeline.definition = withStepPatch(_draft(), id, patch)
}

export function addStep() {
  _pipeline.definition = withAddedStep(_draft())
  const steps = _pipeline.definition.steps
  _pipeline.selectedStepId = String(steps[steps.length - 1].id)
}

export function removeStep(id) {
  _pipeline.definition = withRemovedStep(_draft(), id)
  if (String(_pipeline.selectedStepId) === String(id)) _pipeline.selectedStepId = null
}

/** 画布上的一条连线 → 往 `after` 里加一个步骤 id。**只改这一个字段。** */
export function connectSteps(source, target) {
  const next = withConnection(_draft(), source, target)
  _pipeline.definition = next
}

export function disconnectSteps(source, target) {
  _pipeline.definition = withDisconnection(_draft(), source, target)
}

function _body() {
  return Object.assign({}, _pipeline.definition,
    _pipeline.id === null ? {} : { revision: _pipeline.revision })
}

export async function savePipeline() {
  if (!_pipeline.definition) return null
  _pipeline.saving = true
  _pipeline.error = ''
  try {
    const query = await _projectQuery()
    const body = _body()
    const saved = _pipeline.id === null
      ? await post(`/pipelines${query ? `?${query}` : ''}`, body)
      : await put(`/pipelines/${_pipeline.id}${query ? `?${query}` : ''}`, body)
    _pipeline.id = saved.id
    _pipeline.revision = saved.revision
    _pipeline.saved = _copy(_pipeline.definition)
    pushLog(`管线「${saved.name}」已保存（revision ${saved.revision}）`, 'pipeline')
    await loadPipelines()
    return saved
  } catch (err) {
    _pipeline.error = err.message
    pushLog(`保存管线失败：${err.message}`, 'pipeline', 'error')
    return null
  } finally {
    _pipeline.saving = false
  }
}

export async function archivePipeline(id) {
  try {
    const query = await _projectQuery()
    const archived = await post(
      `/pipelines/${id}/archive${query ? `?${query}` : ''}`, {})
    if (_pipeline.id === archived.id) {
      _pipeline.id = null
      _pipeline.revision = null
      _pipeline.definition = null
      _pipeline.saved = null
    }
    pushLog(`管线「${archived.name}」已归档（没有删除，历史 run 还指着它）`, 'pipeline')
    await loadPipelines()
    return archived
  } catch (err) {
    pushLog(`归档管线失败：${err.message}`, 'pipeline', 'error')
    return null
  }
}

/**
 * 跑一次。
 *
 *   * `dryRun=true` → **同步**返回完整报告，库里一行不留（冻结 F10）。界面拿
 *     它渲染"预演"，与真跑的结果用同一段代码。
 *   * 真跑 → 202 + `run_id`，进度走 SSE 推回来（**界面不自己数数**）。
 */
export async function runPipeline({ dryRun = false, baseDir = null, dataDir = null } = {}) {
  if (!_pipeline.id) return null
  const query = await _projectQuery()
  const body = { dry_run: Boolean(dryRun) }
  if (baseDir) body.base_dir = baseDir
  if (dataDir) body.data_dir = dataDir
  try {
    const data = await post(
      `/pipelines/${_pipeline.id}/run${query ? `?${query}` : ''}`, body)
    if (dryRun) {
      pushLog(`预演完成：${_totalsText(data.report)}（库里一行没写）`, 'pipeline')
      return data
    }
    _pipeline.activeRunId = data.run_id
    _pipeline.progress = { records: 0, line: 0, file: '', done: 0, total: 0 }
    pushLog(`管线已开跑（run ${data.run_id}）`, 'pipeline')
    await loadPipelineRuns(_pipeline.id)
    return data
  } catch (err) {
    pushLog(`跑管线失败：${err.message}`, 'pipeline', 'error')
    return null
  }
}

function _totalsText(report) {
  const totals = (report && report.totals) || {}
  return `新增 ${totals.added || 0} / 更新 ${totals.updated || 0} / 跳过 ${totals.skipped || 0}`
}

/** 订阅一次真跑的进度流。返回一个 `close()`；跑完自动关。 */
export function watchPipelineRun(runId) {
  return watchRun(runId, (payload) => {
    const progress = _pipeline.progress || {}
    _pipeline.progress = {
      records: payload.records !== undefined ? payload.records : progress.records,
      line: payload.line !== undefined ? payload.line : progress.line,
      file: payload.file !== undefined ? payload.file : progress.file,
      done: payload.index !== undefined ? payload.index + 1 : progress.done,
      total: payload.total !== undefined ? payload.total : progress.total,
    }
  }, () => {
    pushLog(`run ${runId} 的事件流结束`, 'pipeline')
    if (_pipeline.id !== null) loadPipelineRuns(_pipeline.id)
  })
}

export async function loadPipelineRuns(id) {
  try {
    const query = await _projectQuery()
    _pipeline.runs = await get(
      `/pipelines/${id}/runs${query ? `?${query}` : ''}`)
  } catch (err) {
    pushLog(`读取运行历史失败：${err.message}`, 'pipeline', 'error')
  }
  return _pipeline.runs
}

export async function undoRun(runId, dryRun) {
  const data = await post(`/runs/${runId}/undo`, { dry_run: Boolean(dryRun) })
  pushLog(`${dryRun ? '预演：' : ''}撤销 run ${runId} —— 撤了 ${(data.undone || []).length} 个作业`
    + (data.stopped_at ? `，停在作业 ${data.stopped_at}` : ''), 'pipeline')
  if (_pipeline.id !== null) await loadPipelineRuns(_pipeline.id)
  return data
}

export async function redoRun(runId, dryRun) {
  const data = await post(`/runs/${runId}/redo`, { dry_run: Boolean(dryRun) })
  pushLog(`${dryRun ? '预演：' : ''}重放 run ${runId} —— 放回 ${(data.redone || []).length} 个作业`
    + (data.stopped_at ? `，停在作业 ${data.stopped_at}` : ''), 'pipeline')
  if (_pipeline.id !== null) await loadPipelineRuns(_pipeline.id)
  return data
}

// ---------------------------------------------------------------------------
// 视图
// ---------------------------------------------------------------------------

export function setView(key) {
  // 切模式**保留草稿**（冻结 F8）：切回来的时候它还在。不丢草稿是因为它本来
  // 就不该丢 —— 也正因为如此，这里不需要一个阻塞式确认弹窗（那种弹窗唯一的
  // 效果是训练人无脑点"确定"）。
  _view.value = key
  if (key === 'pipeline' && _pipeline.definition === null && !_pipeline.loading) {
    // 第一次进编排模式：把列表取回来，并开一份空草稿，别让画布是一片什么都没有
    // 的空白 —— 空白会被读成"坏了"。
    loadPipelines()
    newPipelineDraft()
  }
}
