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
import { del, get, patch, post, put, watchRun } from './api.js'
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
//: `loadProjects` 跑过没有 —— 图 / 镜头 / 标签三个加载器的"空项目守卫"只在
//: **知道**库里是 0 个活跃项目之后才生效（有活跃项目就会被自动选上，所以
//: "loadProjects 跑过 且 还没选中"⟺ 0 个）。不带这个标记时直接调加载器照常
//: 请求 —— 服务端在恰好一个项目时会隐式解析，"不带 project"本身不是错误。
let _projectsLoaded = false
const _meta = ref(null)
//: 镜头载荷，键是镜头 id。**空对象起步**：有哪几个镜头由库里的
//: `kind_registry.lens` 决定（`/meta` 把它带回来），不在这里写死。
const _lenses = reactive({})
const _lensKeys = ref([])
const _openInboxCount = ref(0)

//: `total`（0.20.0）= 服务端同筛选条件、不带 LIMIT 的库中总数（`graph_payload`
//: 的 COUNT）。它和 `nodes.length`（已载入）是**两个口径** —— 截图里"库里有
//: 500 个实体"就是拿后者冒充前者来的。
const _raw = reactive({ nodes: [], edges: [], layout: {}, total: 0 })
//: 聚合卡（`kind:<kind>` 虚拟节点）的**会话级**位置记忆。不入库：`entity_layout`
//: 的外键指向 `entities(id)`（整数实体），而聚合卡既不是实体、`Number('kind:x')`
//: 还是 NaN —— 走持久化链路只会送一个 NaN 出去。换来的行为是"刷新回圆环、
//: session 内（筛选/切页/重算）保持"，而刷新后的确定性圆环本身也是一种稳定布局。
const _aggLayout = reactive({})
//: 标签词表（`GET /tags`）。**空数组起步**，界面上"一个标签都没有"与
//: "还没读到"于是是同一种显示 —— 而那正是想要的：本地工具里没有"加载中"的
//: 那一格，也不该有。
const _tags = ref([])
//: 域名名下资产计数（0.20.0 · `GET /domains/summary`）。**按项目缓存**：读时
//: 比对 `project`，对不上就重取（换项目 = 换了一个库的事实）。取失败缓存空列表
//: 而不是每次重试 —— 下拉少几个选项是降级，每翻一页镜头打一次失败请求是拖累。
//: 下拉的 `key (N)` 与详情面板的名下计数**同吃这一份** —— 各拉各的迟早出现
//: 同一个域名两个不同的 N，而那正是这次要修的病。
const _domainSummary = ref({ project: undefined, rows: [] })
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
//: `underDomain`（0.19.0 域名视图）与 `kinds`（0.20.0 类型筛选）都是**服务端**
//: 参数：`/graph` 写死 `limit=500`，在前端已取回的 500 个节点上做筛选 = 第 501
//: 个资产永远看不见 —— 筛 `secret`（库里 13 个）本地筛得到 0 个，就是截图里
//: 那个 bug。所以两者都进 URL（`under_domain=` / `kinds=`），载图重来；
//: kinds **本地那层仍保留**（同一谓词两层并存）：挡住邻居合并进来的异类节点。
//: keyword/focusId 仍是纯本地筛选（关键词本来就只在载入窗口内搜）。
//: 进入域名视图时清 focusId 是怕"高亮一片 + 缩小一图"被读成两层筛选。
const _filter = reactive({ kinds: [], keyword: '', focusId: null, depth: 1,
                           underDomain: null })
const _logLines = shallowRef([])
//: 默认页 = **项目总览**（评审十一：安全产品不该一进来就是图，先答"这个项目
//: 现在怎么样"）。进画布是总览上的一次显式动作（「进入攻击面画布」/左栏导航）。
const _view = ref('overview')
const _dataRevision = ref(0)
const _paletteOpen = ref(false)
const _workspaceJump = ref(null)
const _drawerOpen = ref(false)
//: 日志抽屉高度（视觉整改：可拖高）。**会话内记忆**，与聚合卡位置同一待遇
//: （不入库 —— 布局偏好不是事实）；clamp 在组件里（96–520，见 LogDrawer）。
//: 默认 130（评审十二：任务中心常驻 100–150px —— 短到不占画布，又留得下
//: 一行进度 + 两行最近任务）。
const _drawerHeight = ref(130)
//: 聚合下钻（评审二/三）：点聚合卡后要"看具体实体"的那一个 kind。会话级 ——
//: 它是**显示状态**不是事实，刷新回聚合；置位期间 `_visible` 旁路
//: `buildAggregate`（否则 endpoint 9,593 会重新聚回一张卡，下钻等于没发生）。
const _drillKind = ref(null)
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

//: 三个口径各说各的（0.20.0 口径修复）：
//:   `graphTotal`  = 符合**当前筛选**的库中总数（服务端 COUNT，无 LIMIT）
//:   `counts.entities` = 画布**已载入**的节点数（≤500 或筛选后的实到数）
//:   `projectTotal` = 这个项目**库里**的实体总数（`/projects` 的 counts ——
//:   与筛选无关，左栏徽标、空状态的"库里有"都用它）
const _graphTotal = computed(() => _raw.total)

const _projectTotal = computed(() => {
  const row = _projects.value.find((p) => p.name === _project.value)
  return row ? Number(row.counts?.entities || 0) : 0
})

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
  // 下钻中（`drillKind`）**旁路聚合**：这一屏的全部意义就是逐个看那一类
  // 实体 —— 此时服务端已按 kinds 裁剪（≤500 个，与平时载入窗口同量级），
  // 再聚一次等于点了没反应。
  if (!_drillKind.value && shouldAggregate(filtered.nodes)) {
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
  // 首页（评审十一）：项目总览 —— 资产四象限 / 风险概览 / 重点风险资产。
  // 排第一：它同时是 TopBar 的第一个页签与默认视图（`_view` 的初值）。
  { key: 'overview', label: '项目总览', hint: '资产规模 + 风险概览 + 重点风险资产', group: 'mode' },
  // 重定位（评审四）：这张图画的是攻击面链路（domain → ip → service →
  // endpoint → secret → vuln），名字要说出来。
  { key: 'explore', label: '攻击面画布', hint: '探索模式（只读）', group: 'mode' },
  // 编排模式只是 `store.view` 的**一个新取值**（冻结 F5）—— 不新增第二套路由
  // 状态。第二套路由状态的表现是"刷新之后回到另一个页面"，而那在本地工具里
  // 会被当成"偶发"。
  { key: 'pipeline', label: '编排', hint: '管线编排（可编辑）', group: 'mode' },
  { key: 'assets', label: '资产', hint: '镜头：域名 / IP / 主机 / 网段', group: 'lens' },
  { key: 'services', label: '服务与中间件', hint: '镜头：服务 / 产品', group: 'lens' },
  { key: 'api', label: 'API 接口', hint: '按主机与路径下钻', group: 'lens' },
  { key: 'js', label: 'JS 资源', hint: '已导入的 JS 地址索引', group: 'lens' },
  { key: 'evidence', label: '证据', hint: '原始定位、归档与脱敏预览', group: 'lens' },
  { key: 'notes', label: '研究笔记', hint: '现有发现通道中的笔记', group: 'lens' },
  { key: 'tasks', label: '研究任务', hint: '人工待办与关联线索', group: 'lens' },
  { key: 'changes', label: '增量变化', hint: '当前有效贡献带来的新增与变化', group: 'lens' },
  { key: 'vuln', label: '漏洞', hint: '镜头：漏洞（nuclei 的命中）', group: 'lens' },
  // 5.5：风险评分 + 攻击面概览。**纯读侧派生**（GET /risk），与镜头平级 ——
  // 它回答的是"这些事实合起来意味着什么"，不是又一个镜头。
  { key: 'risk', label: '风险', hint: '风险评分 + 攻击面概览（零出站）', group: 'gov' },
  { key: 'jobs', label: '作业历史', hint: '导入 / undo / redo', group: 'gov' },
  { key: 'inbox', label: 'Inbox', hint: '待裁决', group: 'gov' },
  { key: 'doctor', label: '数据体检', hint: '完整性 / 孤儿 / 未测', group: 'gov' },
  // 0.21.0：MCP 接入页 —— 配置（接线片段）+ 工具清单（与适配器 tools/list
  // 同源）+ 页内试用。AI 客户端经这一页接进来；写面只有 add_finding 一个。
  { key: 'mcp', label: 'MCP 接入', hint: '本机 MCP 配置 + 工具清单 + 页内试用', group: 'gov' },
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
  dataRevision: _dataRevision,
  paletteOpen: _paletteOpen,
  workspaceJump: _workspaceJump,
  openWorkspace(category, row = null, filters = {}) {
    _workspaceJump.value = { project: _project.value, category, row, filters, sequence: (_workspaceJump.value?.sequence || 0) + 1 }
    setView(category)
  },
  drawerOpen: _drawerOpen,
  drawerHeight: _drawerHeight,
  activeImportId: _activeImportId,
  importProgress: _importProgress,
  importReport: _importReport,
  pipeline: _pipeline,
  tags: _tags,
  domainSummary: _domainSummary,
  selection: _selection,

  // -- 派生 --
  counts: _counts,
  graphTotal: _graphTotal,
  projectTotal: _projectTotal,
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
  archiveProject,
  restoreProject,
  deleteProject,
  selectEntity,
  ensureVisible,
  moveEntity,
  moveAggregate,
  toggleCollapse,
  persistLayout,
  setFilter,
  toggleKind,
  clearFilter,
  drill: _drillKind,
  drillKind,
  exitDrill,
  setDomainFocus,
  clearDomainFocus,
  tagEntity,
  setEntityTags,
  loadTags,
  loadDomainSummary,
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
  // 默认列表**不含归档**（0.20.0，服务端 `GET /projects` 的纪律）。
  _projects.value = await get('/projects')
  _projectsLoaded = true
  if (!_project.value && _projects.value.length) {
    // 有活跃项目就选第一个（0.20.0）：原来 `=== 1`，两个项目启动时谁都不选 →
    // 画布直接吃一个 E_PROJECT_REQUIRED 红错 —— 多项目是项目管理的常态，
    // 不是错误态。CLI 的隐式解析仍要求显式 `--project`（行序 ≠ 意图）；界面
    // 上的"第一个"随时可换，选错的代价只是看错一眼。
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
 * 归档 / 删除之后的**指针维护**：当前项目若从活跃列表里消失（被归档或删掉），
 * 指到剩下的第一个；一个不剩就置空 —— 空项目时 `loadGraph` 的守卫把画布清空，
 * EmptyGuide 的"建项目"分支接管（而不是把"没有项目"渲染成红色错误）。
 */
async function _reseatProject() {
  if (_project.value && !_projects.value.some((p) => p.name === _project.value)) {
    await selectProject(_projects.value[0] ? _projects.value[0].name : '')
  }
}

export async function archiveProject(id) {
  const data = await post(`/projects/${id}/archive`, {})
  await loadProjects()
  await _reseatProject()
  return data
}

export async function restoreProject(id) {
  const data = await post(`/projects/${id}/restore`, {})
  await loadProjects()          // 当前指针不受伤（当前项目本来就 active）；
  return data                   // 若当前为空，loadProjects 顺手选回第一个
}

export async function deleteProject(id, confirm) {
  const data = await del(`/projects/${id}?confirm=${encodeURIComponent(confirm)}`)
  await loadProjects()
  await _reseatProject()
  return data
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
  if (!_project.value && _projectsLoaded) {
    // loadProjects 之后仍没选中 = 0 个活跃项目：镜头内容按项目才有意义，
    // 那些必然 400 的请求不发 —— missing 与 404 同义，页面显示"这个镜头
    // 还是空的"。
    for (const lens of keys) {
      _lenses[lens] = { lens, kinds: [], total: 0, missing: true }
    }
    return
  }
  for (const lens of keys) {
    try {
      _lenses[lens] = await get(`/lenses/${lens}`)
    } catch (err) {
      _lenses[lens] = { lens, kinds: [], total: 0, missing: true }
    }
  }
}

let graphGeneration = 0
export async function loadGraph() {
  const generation = ++graphGeneration
  if (!_project.value && _projectsLoaded) {
    // loadProjects 之后仍没选中 = 0 个活跃项目（空库 / 全部归档）：**别问
    // 服务端要图** —— 不带 project 的 `/graph` 在这种库里是 400
    // E_PROJECT_REQUIRED，把"还没建项目"渲染成红色错误是错的，它其实是
    // 引导卡的第一步。清空画布，EmptyGuide 的建项目分支接管。
    _raw.nodes = []
    _raw.edges = []
    _raw.layout = {}
    _raw.total = 0
    _loadError.value = ''
    _loading.value = false
    return
  }
  _loading.value = true
  _loadError.value = ''
  try {
    const query = await _projectQuery()
    if (generation !== graphGeneration) return
    const domain = _filter.underDomain
      ? `&under_domain=${encodeURIComponent(_filter.underDomain)}` : ''
    //: kinds 进 URL（0.20.0）：与 under_domain 同一条纪律 —— 筛 secret 时那 13
    //: 个多半不在前 500 里，本地筛等于"0 个匹配"（截图 bug 的根因）。
    const kinds = _filter.kinds.length
      ? `&kinds=${encodeURIComponent(_filter.kinds.join(','))}` : ''
    const data = await get(`/graph?limit=500${query ? `&${query}` : ''}${domain}${kinds}`)
    if (generation !== graphGeneration) return
    _raw.nodes = data.nodes || []
    _raw.edges = data.edges || []
    _raw.layout = data.layout || {}
    //: total 缺省时退回"已载入数"—— 老后端没有这个键，退化的口径至少不会凭空
    //: 多出一个更大的数。
    _raw.total = Number.isFinite(Number(data.total))
      ? Number(data.total) : _raw.nodes.length
  } catch (err) {
    if (generation !== graphGeneration) return
    _loadError.value = err.message
    _raw.nodes = []
    _raw.edges = []
    _raw.layout = {}
    _raw.total = 0
  } finally {
    if (generation === graphGeneration) _loading.value = false
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
  // The research workspace does not need graph rows. Load its bounded window
  // only when the user explicitly enters the canvas.
  await Promise.all([_view.value === 'explore' ? loadGraph() : Promise.resolve(), loadLenses(), loadTags()])
  _dataRevision.value++
}

export async function selectProject(name) {
  // 换项目时清空未提交的布局攒批，避免上个项目的节点坐标被 debounced patch 送到新项目
  if (layoutTimer) {
    clearTimeout(layoutTimer)
    layoutTimer = null
  }
  pendingLayout.clear()

  _project.value = name
  _workspaceJump.value = null
  graphGeneration++
  _raw.nodes = []; _raw.edges = []; _raw.layout = {}; _raw.total = 0
  _loadError.value = ''; _loading.value = false
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
  // 下钻状态同理：B 项目未必有这个 kind，带着它 = 一屏空图 + 一个说不清
  // 来历的"下钻中"横幅。
  _drillKind.value = null
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
    //: 点开的是域名 → 顺手把名下计数拉回来（按项目缓存，整个项目只拉一次）。
    //: 触发点放在 store 而不是 DetailPanel 里：组件的 watch/挂载发请求会打爆
    //: 「复制给 AI」行为测试钉死的 get ×1 —— 那条断言守的是"复制这个动作不附带
    //: 别的请求"，而直接往 `selectedDetail` 塞值的测试不经过这里，互不相扰。
    //: 不 await：详情立刻打开，计数行是随缓存到位**补上**的，不是前置门槛。
    if (detail?.entity?.kind === 'domain') loadDomainSummary()
    return _selectedDetail.value
  } catch (err) {
    pushLog(`读取实体 ${id} 失败：${err.message}`, 'import', 'error')
    return null
  } finally {
    _selectedLoading.value = false
  }
}

/**
 * 关系跳转的兜底（0.20.0 修"坏链"）：目标不在载入窗口（`limit=500`）时，
 * 拉它的邻居 `GET /graph/{id}/neighbors?depth=1`，**按 id 去重合并**进 `_raw`，
 * 然后照常选中 —— 详情面板点一条关系不再以"画布上找不到"告终。
 *
 * 合并函数刻意叫 `_mergeGraphFragment` 而不是别的什么：READONLY 红线扫
 * `frontend/src/**`，`addNode` / `setNodes` 这些名字一出现整轮验证变红。
 * 这里合并的是**服务端返回的既有事实**（邻居读），不是"往图里造东西"。
 *
 * 合入后仍可能不被渲染（kind 不在当前筛选里、或进了聚合卡）—— 那时详情
 * 照样打开，并在日志里说清楚"详情已打开，但它不在当前画布窗口"。
 */
function _mergeGraphFragment(data) {
  const seenNodes = new Set(_raw.nodes.map((n) => String(n.id)))
  for (const node of (data.nodes || [])) {
    if (seenNodes.has(String(node.id))) continue
    seenNodes.add(String(node.id))
    _raw.nodes.push(node)
  }
  const seenEdges = new Set(_raw.edges.map((e) => String(e.id)))
  for (const edge of (data.edges || [])) {
    if (seenEdges.has(String(edge.id))) continue
    seenEdges.add(String(edge.id))
    _raw.edges.push(edge)
  }
  // 布局**只补空缺**：邻居 payload 里的坐标不覆盖本地已有的（拖过的位置是
  // 人刚决定的，比服务端那份新）。
  for (const [key, pos] of Object.entries(data.layout || {})) {
    if (!_raw.layout[key]) _raw.layout[key] = pos
  }
}

export async function ensureVisible(id) {
  const loaded = _raw.nodes.some((n) => String(n.id) === String(id))
  if (!loaded) {
    try {
      const query = await _projectQuery()
      const data = await get(`/graph/${id}/neighbors?depth=1`
                             + `${query ? `&${query}` : ''}`)
      _mergeGraphFragment(data)
    } catch (err) {
      pushLog(`拉取实体 ${id} 的邻居失败：${err.message}`, 'activity', 'warn')
    }
  }
  _filter.focusId = null          // 邻域高亮与"跳过去"是两层语义，别叠
  await selectEntity(id)
  const rendered = _visible.value.nodes.some(
    (n) => String(n.data?.entityId ?? '') === String(id))
  if (!rendered) {
    pushLog('详情已打开 —— 该实体不在当前画布窗口（被筛选挡住或进了聚合卡）',
            'activity', 'warn')
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

/**
 * 类型筛选。**先改本地、再重载**（0.20.0）：本地那层谓词立刻生效（下面
 * `_visible` 同步重算，测试与交互都不等网络），重载把 kinds 推给服务端 ——
 * 截图里筛 `secret` 得到 0 个，就是因为只改了本地、而那 13 个不在前 500 里。
 */
/**
 * 聚合卡下钻（评审二/三：「domain × 30」点开要看见那 30 个域名）。
 *
 * 两步合成一次动作：
 *   1. 服务端 `kinds=[kind]` 重载 —— 拿到的**最多 500 个该类型实体**，
 *      而不是"已载入窗口里恰好摊到的那几个"（窗口裁剪会让下钻看起来少人）；
 *   2. `_drillKind` 置位 —— `_visible` 从此旁路聚合，哪怕这一类有 9,593 个
 *      （服务端 limit 兜底 500，与平时的载入窗口同量级，不会炸渲染）。
 *
 * 退出走 `exitDrill`（横幅按钮），或任何一次手动改筛选（`toggleKind` /
 * `clearFilter` 顺带清标记 —— 那时用户已经改了主意，留着下钻状态只会让
 * "我明明点了取消怎么还是单类型视图"变成一个谜）。
 */
export async function drillKind(kind) {
  const value = String(kind || '').trim()
  if (!value) return
  _drillKind.value = value
  if (_filter.kinds.length !== 1 || _filter.kinds[0] !== value) {
    _filter.kinds = [value]
  }
  pushLog(`下钻 ${value}：聚合卡展开为逐实体视图`, 'activity')
  await loadGraph()
}

export async function exitDrill() {
  if (!_drillKind.value) return
  const was = _drillKind.value
  _drillKind.value = null
  if (_filter.kinds.length === 1 && _filter.kinds[0] === was) {
    _filter.kinds = []
    await loadGraph()
  }
}

export async function toggleKind(kind) {
  _drillKind.value = null          // 手动改筛选 = 退出下钻（见 drillKind 注释）
  const set = new Set(_filter.kinds)
  if (set.has(kind)) set.delete(kind)
  else set.add(kind)
  _filter.kinds = [...set]
  await loadGraph()
}

export async function clearFilter() {
  //: kinds / 域名视图都是服务端筛的 —— 只清本地标志不重载，画布上留着的
  //: 还是裁剪过的图。
  const scoped = _filter.underDomain !== null || _filter.kinds.length > 0
  _drillKind.value = null
  _filter.kinds = []
  _filter.keyword = ''
  _filter.focusId = null
  _filter.depth = 1
  _filter.underDomain = null
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
  if (!_project.value && _projectsLoaded) {
    // 0 个活跃项目（loadProjects 之后仍没选中）：词表按项目才有意义。不发
    // 那条必然 400 的请求，也就不往日志里塞一条"读取标签词表失败"——
    // 空库启动的控制台该是安静的。
    _tags.value = []
    return _tags.value
  }
  try {
    const query = await _projectQuery()
    _tags.value = await get(`/tags${query ? `?${query}` : ''}`)
  } catch (err) {
    pushLog(`读取标签词表失败：${err.message}`, 'import', 'warn')
  }
  return _tags.value
}

/**
 * 域名名下资产计数（0.20.0 · `GET /domains/summary`）：**懒加载**，按项目缓存
 * （见 `_domainSummary`）。第一次聚焦域名下拉、或点开一个域名实体时才拉，
 * 之后整个项目内复用 —— 镜头页下拉的 `key (N)` 与详情面板的名下计数同吃
 * 这一份（同源才同数）。
 *
 * 静默失败 —— 下拉只剩「全部」是降级，整页报错是事故。
 */
export async function loadDomainSummary() {
  if (_domainSummary.value.project !== _project.value) {
    try {
      const query = await _projectQuery()
      const data = await get(`/domains/summary${query ? `?${query}` : ''}`)
      _domainSummary.value = { project: _project.value, rows: data || [] }
    } catch (err) {
      pushLog(`读取域名资产计数失败：${err.message}`, 'import', 'warn')
      _domainSummary.value = { project: _project.value, rows: [] }
    }
  }
  return _domainSummary.value.rows
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

export async function submitImport({ paths, parser, dryRun, archive, mapping, expectedSha256 }) {
  const body = { paths, dry_run: Boolean(dryRun), archive: archive || 'archived' }
  if (parser) body.parser = parser
  if (mapping !== undefined) body.mapping = mapping
  if (expectedSha256) body.expected_sha256 = expectedSha256
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
export async function listJobs(limit = 100) {
  const query = await _projectQuery()
  return get(`/jobs?limit=${limit}${query ? `&${query}` : ''}`)
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
  if (key === 'explore') loadGraph()
  if (key === 'pipeline' && _pipeline.definition === null && !_pipeline.loading) {
    // 第一次进编排模式：把列表取回来，并开一份空草稿，别让画布是一片什么都没有
    // 的空白 —— 空白会被读成"坏了"。
    loadPipelines()
    newPipelineDraft()
  }
}
