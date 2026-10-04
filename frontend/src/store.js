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
 *     折叠    → `toggleCollapse`（同样落 `entity_layout`，与位置互不串位）
 *     筛选    → `setFilter` / `toggleKind` / `clearFilter`
 *     标注    → `setEntityTags`（`PATCH /entities/{id}` 的 add_tags / remove_tags）
 *
 * 3.8 给探索模式补的三件事（它们**操作的是实体**，所以落在这里而不是编排模式）：
 *
 *     批量改状态 → `setLifecycle`（`POST /entities/lifecycle`，一个 job，可撤销）
 *     合并 / 拆开 → `mergeEntities` / `unmergeEntity`（`ops.merge` / `ops.unmerge`）
 *     标签词表   → `loadTags`（`GET /tags`，只算活标签）
 *
 * 它们都**只是发请求**：写仍然只经服务端的单写队列，`api/routes.py` 里一条 SQL
 * 都没有（SQL-FREE 红线）。这个模块不写库，过去不，现在也不。
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
//: 聚合卡（`kind:<kind>` 虚拟节点）的**会话级**位置记忆。不入库：`entity_layout`
//: 的外键指向 `entities(id)`（整数实体），而聚合卡既不是实体、`Number('kind:x')`
//: 还是 NaN —— 走持久化链路只会送一个 NaN 出去。换来的行为是"刷新回圆环、
//: session 内（筛选/切页/重算）保持"，而刷新后的确定性圆环本身也是一种稳定布局。
const _aggLayout = reactive({})
//: 标签词表（`GET /tags`）。**空数组起步**，界面上"一个标签都没有"与
//: "还没读到"于是是同一种显示 —— 而那正是想要的：本地工具里没有"加载中"的
//: 那一格，也不该有。
const _tags = ref([])
//: 镜头页域名下拉的选项（0.19.0）。**按项目缓存**：读时比对 `project`，对
//: 不上就重取（换项目 = 换了一个库的事实）。取失败缓存空列表而不是每次重试
//: —— 下拉少几个选项是降级，每翻一页镜头打一次失败请求是拖累。
const _domains = ref({ project: undefined, rows: [] })
//: 批量选中的实体 id。批量改状态的操作对象就是它。
const _selection = ref([])
const _loading = ref(false)
const _loadError = ref('')
const _selectedId = ref(null)
const _selectedDetail = ref(null)
//: 选中实体参与过的合并事件（`GET /entities/{id}/merges`）。详情面板的"拆开"
//: 按钮要的是 `event_id` —— 它是**单独一条读**而不是塞进 `/entities/{id}`，
//: 因为后者的形状是对外契约，3.8 不动它。
const _selectedMerges = ref([])
const _selectedLoading = ref(false)
//: `underDomain`（0.19.0 域名视图）是**服务端**参数：`/graph` 写死
//: `limit=500`，在前端已取回的 500 个节点上做归属筛选 = 第 501 个资产永远
//: 看不见。所以它进 URL（`under_domain=`），载图重来；kinds/keyword/focusId
//: 仍是本地筛选，两层并存互不干涉（进入域名视图时清 focusId 是怕"高亮一片
//: + 缩小一图"被读成两层筛选）。
const _filter = reactive({ kinds: [], keyword: '', focusId: null, depth: 1,
                           underDomain: null })
const _logLines = shallowRef([])
const _view = ref('explore')
const _paletteOpen = ref(false)
const _drawerOpen = ref(true)
//: 日志抽屉高度（视觉整改：可拖高）。**会话内记忆**，与聚合卡位置同一待遇
//: （不入库 —— 布局偏好不是事实）；clamp 在组件里（96–520，见 LogDrawer）。
const _drawerHeight = ref(190)
const _activeImportId = ref(null)
//: 进度数字的口径（0.18.0 多文件）：
//:   `records` / `badLines` 是**跨文件累计**（= baseRecords/baseBadLines +
//:   当前文件已推进的部分）—— 逐文件的数会"完成一个文件进度条归零"，那正是
//:   多文件导入看起来"内容丢了"的一半原因。base* 只在 `file_done` 里加，
//:   加的仍是**数据层给的数**（counts.records），界面不自己数数。
//:   `line` 是**当前文件**的行号（数据层逐文件重置，累计行没有任何权威来源）。
const _importProgress = reactive({
  state: '', records: 0, line: 0, badLines: 0, stage: '', dropped: 0,
  fileIndex: 0, fileTotal: 0, baseRecords: 0, baseBadLines: 0,
  files: [],            // 每个 file_done 一行：{name, path, state, job_id, counts}
  badLinesDetail: [],   // 流式坏行明细 {file, line, reason, raw}（部分，见 LogDrawer）
})
//: 一次导入的完整报告（`GET /imports/{id}`）。后端**从 0.2.0 起就返回它**，
//: 前端一直没消费 —— "多文件导入后内容无法查看"缺的就是这一段接线。
const _importReport = ref(null)

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
    const grouped = buildAggregate(filtered.nodes, filtered.edges, { saved: _aggLayout })
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

// `group` 是**导航分组的展示元数据**（顶栏按它加分隔线：模式 / 镜头 / 治理），
// 只有 TopBar 消费。它不参与任何路由或状态判断 —— 分组乱了顶栏难读，
// 但页面该开还是开；写在这里而不是让 TopBar 自己维护一份 key 清单，
// 是因为两份清单迟早分叉，而症状会是某个页签静默掉组。
export const VIEWS = [
  { key: 'explore', label: '画布', hint: '探索模式（只读）', group: 'mode' },
  // 编排模式只是 `store.view` 的**一个新取值**（冻结 F5）—— 不新增第二套路由
  // 状态。第二套路由状态的表现是"刷新之后回到另一个页面"，而那在本地工具里
  // 会被当成"偶发"。
  { key: 'pipeline', label: '编排', hint: '管线编排（可编辑）', group: 'mode' },
  { key: 'assets', label: '资产', hint: '镜头：域名 / IP / 主机 / 网段', group: 'lens' },
  { key: 'services', label: '服务与中间件', hint: '镜头：服务 / 产品', group: 'lens' },
  { key: 'vuln', label: '漏洞', hint: '镜头：漏洞（nuclei 的命中）', group: 'lens' },
  // 5.5：风险评分 + 攻击面概览。**纯读侧派生**（GET /risk），与镜头平级 ——
  // 它回答的是"这些事实合起来意味着什么"，不是又一个镜头。
  { key: 'risk', label: '风险', hint: '风险评分 + 攻击面概览（零出站）', group: 'gov' },
  { key: 'jobs', label: '作业历史', hint: '导入 / undo / redo', group: 'gov' },
  { key: 'inbox', label: 'Inbox', hint: '待裁决', group: 'gov' },
  { key: 'doctor', label: '数据体检', hint: '完整性 / 孤儿 / 未测', group: 'gov' },
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
  selectedMerges: _selectedMerges,
  selectedLoading: _selectedLoading,
  filter: _filter,
  logLines: _logLines,
  view: _view,
  paletteOpen: _paletteOpen,
  drawerOpen: _drawerOpen,
  drawerHeight: _drawerHeight,
  activeImportId: _activeImportId,
  importProgress: _importProgress,
  importReport: _importReport,
  pipeline: _pipeline,
  tags: _tags,
  domains: _domains,
  selection: _selection,

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
  moveAggregate,
  toggleCollapse,
  persistLayout,
  setFilter,
  toggleKind,
  clearFilter,
  setDomainFocus,
  clearDomainFocus,
  tagEntity,
  setEntityTags,
  loadTags,
  loadDomains,
  // 合并 / 拆开 / 批量改状态（3.8，全部落在探索模式）
  loadMerges,
  mergeEntities,
  unmergeEntity,
  setLifecycle,
  setSelection,
  toggleSelection,
  clearSelection,
  submitImport,
  resetImportProgress,
  listJobs,
  jobDetail,
  undoJob,
  redoJob,
  undoImport,
  loadImportReport,
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
    const domain = _filter.underDomain
      ? `&under_domain=${encodeURIComponent(_filter.underDomain)}` : ''
    const data = await get(`/graph?limit=500${query ? `&${query}` : ''}${domain}`)
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
  // 标签词表与图/镜头一起取（三个都是"这个库里现在有什么"）—— 串行会让首屏
  // 多两个 RTT，而它们之间没有先后依赖。
  await Promise.all([loadGraph(), loadLenses(), loadTags()])
}

export async function selectProject(name) {
  _project.value = name
  _selectedId.value = null
  _selectedDetail.value = null
  _selectedMerges.value = []
  // 选中集合是**项目内的东西**：带过去就是一批指向别的项目的 id，而"批量改
  // 状态"会在下一次点击时把它们送出去。与草稿同一条理由（冻结 F8 第 3 条）。
  _selection.value = []
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
  // 聚合卡位置是**项目内**的会话记忆：带去别的项目，就是把 A 项目的排版
  // 盖在 B 项目的卡上（kind 相同就能撞键）。
  for (const key of Object.keys(_aggLayout)) delete _aggLayout[key]
  // 域名视图同理：A 项目的域名在 B 项目多半查无此人，带着它换项目 = 一张
  // 永远空着的图 + 一个说不清原因的横幅。
  _filter.underDomain = null
  await reload()
}

// ---------------------------------------------------------------------------
// 选择 / 详情
// ---------------------------------------------------------------------------

export async function selectEntity(id) {
  _selectedId.value = id
  _selectedDetail.value = null
  _selectedMerges.value = []
  if (id === null || id === undefined) return null
  if (String(id).startsWith('kind:')) return null      // 聚合节点没有详情
  _selectedLoading.value = true
  try {
    const [detail, merges] = await Promise.all([
      get(`/entities/${id}`), loadMerges(id)])
    _selectedDetail.value = detail
    _selectedMerges.value = merges || []
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

/**
 * 拖**聚合卡**（`kind:` 虚拟节点）。只写会话记忆 `_aggLayout`，不落库 ——
 * 聚合卡进不了 `entity_layout`（见 `_aggLayout` 处的注释），走 `persistLayout`
 * 会把 `Number('kind:domain')` = NaN 送进 PATCH。
 */
export function moveAggregate(id, position) {
  if (!position || !Number.isFinite(position.x) || !Number.isFinite(position.y)) return
  _aggLayout[String(id)] = { x: position.x, y: position.y }
}

/** 折叠。同上：它影响**怎么显示**，不影响**有什么**。 */
export function toggleCollapse(id, position) {
  const key = String(id)
  const current = _raw.layout[key] || {}
  const state = !current.collapsed
  // 坐标**优先取调用方给的那一帧**（那就是屏幕上它现在待着的地方），其次取本地
  // 已经记着的。两者都没有（一个从没拖过、也没人告诉过我们它在哪的节点）时，
  // 这一次折叠只改本地、不落库 —— 见 `queueLayout`。
  const x = Number.isFinite(position?.x) ? position.x : current.x
  const y = Number.isFinite(position?.y) ? position.y : current.y
  _raw.layout[key] = { x: x ?? 0, y: y ?? 0, collapsed: state }
  queueLayout(id, Object.assign({ collapsed: state },
    Number.isFinite(x) && Number.isFinite(y) ? { x, y } : {}))
  return state
}

const LAYOUT_FLUSH_MS = 400
let layoutTimer = null
let pendingLayout = new Map()

/** 把一条布局修改并进攒批。**按列并集**：同一节点先拖后折叠，两次的字段要合
 *  起来，而不是后一次把前一次覆盖掉 —— 那正是"两列串位"的本地版本。 */
function queueLayout(id, fields, { immediate = false } = {}) {
  const key = String(id)
  const merged = Object.assign({}, pendingLayout.get(key), fields)
  if (!Number.isFinite(merged.x) || !Number.isFinite(merged.y)) {
    // 没有坐标的写入**不落库**（本地状态已经改了，界面不会骗人）。
    //
    // 两个理由，第二个是决定性的：
    //   1. 后端对"给一条不存在的行只写 collapsed"是拒绝的 —— 送过去只会换回
    //      一个 400 与一行红色日志。
    //   2. 万一那条规则哪天松了，写出去的会是一条**位于原点**的记录。读侧把
    //      它当成"人把这个节点拖到了左上角"，于是节点下次打开就跳过去 ——
    //      一个看起来像随机布局漂移的 bug，比"这一次折叠没存上"难查得多。
    return
  }
  pendingLayout.set(key, Object.assign({ entity_id: Number(id) }, merged))
  if (layoutTimer) clearTimeout(layoutTimer)
  if (immediate) {
    flushLayout()
    return
  }
  layoutTimer = setTimeout(flushLayout, LAYOUT_FLUSH_MS)
}

async function flushLayout() {
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

/**
 * 把本地坐标写回 `entity_layout`（§7.3 防视觉混乱第 ③ 条）。
 *
 * **合并 + 去抖**：拖一次节点会来几十个 move 事件，一个一个 PATCH 会把单写
 * 队列塞满 —— 而写队列是导入共用的那一条。位置这种"最后一帧才算数"的数据，
 * 合并是唯一正确的攒批方式。
 *
 * **只送坐标，不送 collapsed**（3.8）：整行覆盖写会让"拖一下"把折叠状态一起
 * 抹掉 —— 位置与折叠是两个互不相干的人工决定，各有各的入口。
 */
export function persistLayout(id, position, { immediate = false } = {}) {
  moveEntity(id, position)
  queueLayout(id, { x: position.x, y: position.y }, { immediate })
}

export function setFilter(next) {
  if (next.kinds !== undefined) _filter.kinds = next.kinds
  if (next.keyword !== undefined) _filter.keyword = next.keyword
  if (next.focusId !== undefined) _filter.focusId = next.focusId
  if (next.depth !== undefined) _filter.depth = next.depth
  if (next.underDomain !== undefined) _filter.underDomain = next.underDomain
}

/**
 * 进入域名视图（0.19.0）：置位 + 清邻域高亮 + **重新载图**。
 *
 * 重载是必须的 —— 归属推导在服务端（`limit=500` 的道理见 `_filter` 注释），
 * 只改本地状态的话画布上还是全量图。
 */
export async function setDomainFocus(key) {
  _filter.underDomain = key || null
  _filter.focusId = null
  await loadGraph()
}

/** 退出域名视图：回到全量图（其它筛选原样保留）。 */
export async function clearDomainFocus() {
  if (_filter.underDomain === null) return
  _filter.underDomain = null
  await loadGraph()
}

export function toggleKind(kind) {
  const set = new Set(_filter.kinds)
  if (set.has(kind)) set.delete(kind)
  else set.add(kind)
  _filter.kinds = [...set]
}

export async function clearFilter() {
  const scoped = _filter.underDomain !== null
  _filter.kinds = []
  _filter.keyword = ''
  _filter.focusId = null
  _filter.depth = 1
  _filter.underDomain = null
  // 域名视图是服务端筛的 —— 只清本地标志不重载，画布上留着的还是裁剪过的图。
  if (scoped) await loadGraph()
}

/** 标注：加一个标签。走 2.7 就有的 `PATCH /entities/{id}`（写值那条路仍然 501）。 */
export async function tagEntity(id, tag) {
  return setEntityTags(id, { add: [tag] })
}

/**
 * 标注的**完整**入口（3.8 把"删"补上 —— 旧实现只有加，于是"增删改"里的删
 * 只能靠人记得去别处做）。
 *
 * 空数组不算一次写入：`PATCH /entities/{id}` 对"什么都没改"是 400，这不是
 * 故障，是那条路由在防"一次静默的空写"。这里先挡掉，免得点一下空白按钮就
 * 在日志里留一条红色。
 */
export async function setEntityTags(id, { add = [], remove = [], reason = null } = {}) {
  if (!add.length && !remove.length) return null
  const body = {}
  if (add.length) body.add_tags = add
  if (remove.length) body.remove_tags = remove
  if (reason) body.reason = reason
  const data = await patch(`/entities/${id}`, body)
  if (_selectedDetail.value && _selectedDetail.value.entity.id === id) {
    _selectedDetail.value = Object.assign({}, _selectedDetail.value, { tags: data.tags })
  }
  pushLog(`实体 ${id} 的标签：+${add.length} / -${remove.length}`, 'import')
  await loadTags()
  return data
}

/** 标签词表（项目里已经用过的词 + 计数）。**显示的是库里的数。** */
export async function loadTags() {
  try {
    const query = await _projectQuery()
    _tags.value = await get(`/tags${query ? `?${query}` : ''}`)
  } catch (err) {
    pushLog(`读取标签词表失败：${err.message}`, 'import', 'warn')
  }
  return _tags.value
}

/**
 * 镜头页域名下拉的选项（0.19.0）：**懒加载**，带项目级缓存（见 `_domains`）。
 * 静默失败 —— 下拉只剩「全部」是降级，整页报错是事故。
 */
export async function loadDomains() {
  if (_domains.value.project !== _project.value) {
    try {
      const query = await _projectQuery()
      const data = await get(
        `/entities?kind=domain&limit=500${query ? `&${query}` : ''}`)
      _domains.value = { project: _project.value, rows: data }
    } catch (err) {
      pushLog(`读取域名清单失败：${err.message}`, 'import', 'warn')
      _domains.value = { project: _project.value, rows: [] }
    }
  }
  return _domains.value.rows
}

// ---------------------------------------------------------------------------
// 合并 / 拆开 / 批量改状态（3.8）—— 都是**实体**上的写，落在探索模式
// ---------------------------------------------------------------------------
//
// 三条纪律，都不是界面的口味问题：
//
//   * 合并**只能**走 `ops.merge`（经 `POST /entities/{id}/merge`）。在界面里
//     拼一次"把两个卡片画成一个"会同时丢掉可逆性与冲突登记，而症状要等到
//     有人想拆开的时候才出现。
//   * 拆开**只能**走 `ops.unmerge`。它天然精确可逆，因为它**没有移动过任何
//     事实**（INV-10）—— 事实仍在原来那个实体上，merge 只是一层身份覆盖。
//   * 批量改状态是**一个 job**（一次点击 = 一次事务），撤销走 `POST /jobs/{id}/undo`
//     —— 与作业历史页上的撤销是同一个按钮背后的同一段代码。

export async function loadMerges(entityId) {
  try {
    return await get(`/entities/${entityId}/merges`)
  } catch (err) {
    pushLog(`读取合并事件失败：${err.message}`, 'import', 'warn')
    return []
  }
}

/**
 * 合并。**幸存者由界面明确指定**（选中哪个节点当幸存者是一次人工决定，
 * 不让代码去猜"谁更像主"—— 猜错的合并比没合并糟得多，因为它的症状要等到
 * 有人想拆开时才出现）。
 *
 * `survivorKey` 与 `survivorId` 都要：id 是路由的入口，key 是 `ops.merge`
 * 的入口，而**服务端会核对两者一致**（`E_MERGE_SURVIVOR_MISMATCH`）——
 * 那个核对就是防止"界面拿错了 id"这类错误静默地合并错东西。
 */
export async function mergeEntities({ survivorId, survivorKey, memberKeys,
                                      dryRun = false } = {}) {
  if (!survivorKey || !memberKeys || !memberKeys.length) {
    return null
  }
  const query = await _projectQuery()
  const body = { survivor_key: survivorKey, member_keys: memberKeys, dry_run: dryRun }
  const data = await post(
    `/entities/${survivorId}/merge${query ? `?${query}` : ''}`, body)
  pushLog(`${dryRun ? '预演：' : ''}合并了 ${memberKeys.length} 个实体到 ${survivorKey}`
    + (dryRun ? '' : `（事件 ${data.event_id}，冲突 ${data.conflicts.length} 条）`),
    'import')
  if (!dryRun) await reload()
  return data
}

export async function unmergeEntity(entityId, eventId, dryRun = false) {
  const query = await _projectQuery()
  const data = await post(
    `/entities/${entityId}/unmerge${query ? `?${query}` : ''}`,
    { event_id: eventId, dry_run: dryRun })
  pushLog(`${dryRun ? '预演：' : ''}拆开了合并事件 ${eventId}`, 'import')
  if (!dryRun) await reload()
  return data
}

/**
 * 批量改状态。**一个 job**（`ops.set_lifecycle` 的段落注释解释了为什么不是
 * N 个），撤销就是作业历史里那一个 job 的撤销按钮。
 */
export async function setLifecycle(entityIds, lifecycle, { reason = null, dryRun = false } = {}) {
  const ids = [...new Set((entityIds || [])
    .filter((v) => v !== null && v !== undefined && v !== '')
    .map(Number).filter((n) => Number.isFinite(n)))]
  if (!ids.length) return null
  const query = await _projectQuery()
  const body = { entity_ids: ids, lifecycle, dry_run: Boolean(dryRun) }
  if (reason) body.reason = reason
  const data = await post(
    `/entities/lifecycle${query ? `?${query}` : ''}`, body)
  pushLog(`${dryRun ? '预演：' : ''}${ids.length} 个实体 → ${lifecycle}`
    + `（改了 ${data.counts.changed} 个，job ${data.job_id}）`, 'import')
  if (!dryRun) await reload()
  return data
}

/** 批量选中（3.8）：画布支持 `Shift` 框选与多选，这些 id 就是"批量改状态"的操作对象。
 *  **只在这里存一份**，节点上的选中态由它算出来 —— 两份选中状态迟早会分叉。 */
export function setSelection(ids) {
  // `Number(null)` 是 0、`Number('')` 也是 0 —— 只判 `Number.isFinite` 会让
  // 一个空值悄悄变成"选中实体 0"。所以空值先剔掉，再谈数字。
  _selection.value = [...new Set(
    (ids || []).filter((v) => v !== null && v !== undefined && v !== '')
      .map(Number).filter(Number.isFinite))]
  return _selection.value
}

export function toggleSelection(id) {
  const key = Number(id)
  if (id === null || id === undefined || id === '' || !Number.isFinite(key)) {
    return _selection.value
  }
  const set = new Set(_selection.value)
  if (set.has(key)) set.delete(key)
  else set.add(key)
  return setSelection([...set])
}

export function clearSelection() {
  return setSelection([])
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
  _importProgress.fileIndex = 0
  _importProgress.fileTotal = 0
  _importProgress.baseRecords = 0
  _importProgress.baseBadLines = 0
  _importProgress.files = []
  _importProgress.badLinesDetail = []
  _importReport.value = null
}

/**
 * 拉一次导入的完整报告（0.18.0：前端**首次**消费 `GET /imports/{id}` 的
 * `report.files[]`）。`done` 事件只带 job_ids 与 totals —— 逐文件的
 * path/state/counts 只在报告里，而"三个文件导完哪个没成"恰恰只能看它。
 * 报告拉不到（登记表把记录 prune 了）不报错：报告是**补看**的，
 * 进度与日志已经在事件流里说完了。
 */
export async function loadImportReport(importId) {
  try {
    const record = await get(`/imports/${encodeURIComponent(importId)}`)
    _importReport.value = record && record.report ? record : null
  } catch (err) {
    pushLog(`导入报告拉取失败：${err.message}`, 'import', 'warn')
    _importReport.value = null
  }
  return _importReport.value
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
