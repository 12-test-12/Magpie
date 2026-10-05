<script setup>
/**
 * 右栏：实体详情（计划书 §7.1 的"节点参数面板"）。
 *
 * 区块主干（GPT 评审八的"安全详情页"）：
 *
 *   基本信息 / 来源（观测 + 时间线）/ 风险路径 / 关联资产（域名视图 + 关系）
 *   / 标签·合并（维护）/ AI分析（复制给 AI，secret 先过二次确认）
 *
 * 观测按 origin 分组折叠；敏感观测值默认 `前4位****` 预览（评审七 ——
 * 见 `looksSensitive` 上方那段：原文从未入库，这里防的是漏网）。
 *
 * 这里的「观测按 type 分组」用的是 `observations.origin`（tool / manual /
 * imported）—— 它是数据层里**真的**存在的分组，不是界面自己按字段名前缀猜的。
 * 猜一个分组出来会让人以为"这个组是数据层的概念"，而它不是。
 *
 * 本阶段详情面板是**只读 + 标注 + 三个实体操作**（3.8）：标签可以加也可以删
 * （`PATCH /entities/{id}` 的 add_tags / remove_tags）；**字段的值不能改** ——
 * 那条路由今天返回 501，理由是"人工写值"必须走 `ManualWriter` 的值写入路径
 * （2.6）。界面把这件事说出来，而不是给一个点了报错的输入框。
 *
 * 3.8 加的三件事都**不是**在界面里拼出来的：
 *
 *   改状态   → `store.setLifecycle`（`POST /entities/lifecycle`，一个 job）
 *   合并     → `store.mergeEntities`（`ops.merge`，可 unmerge）
 *   拆开     → `store.unmergeEntity`（`ops.unmerge`，天然精确可逆）
 *
 * 合并的**幸存者由人指定**（"合并到这个"按钮），不让界面去猜谁更像主 ——
 * 猜错的合并比没合并糟得多，因为它的症状要等到有人想拆开时才出现。
 */
import { computed, ref, watch } from 'vue'
import { get } from '../api.js'
import { store } from '../store.js'
import { kindColor } from '../graph.js'
import { fmtInt } from '../format.js'

const detail = computed(() => store.selectedDetail)

// ---------------------------------------------------------------------------
// 默认态：项目概览（GPT ④⑫）。未选中实体时右栏不再是一句说明文字，而是
// 「风险分 + 类型分布 + 脚注」的概览卡。
// ---------------------------------------------------------------------------

/**
 * 风险分来自 `GET /risk`（风险页同一份数据，界面不自算 —— 评分口径只有
 * `core/risk` 一处）。
 *
 * **懒加载**：只在右栏处于默认态、且这个项目还没拉过时才发请求（按项目记账，
 * 切项目重拉）。选中实体时一个请求都不发 —— 「复制给 AI」的行为测试把
 * `get` 的调用次数钉死为 1，概览卡若在选中态也拉 `/risk` 会把那条断言变红，
 * 而它红的原因与复制毫无关系。
 */
const riskPayload = ref(null)
const riskProject = ref(null)
const riskError = ref('')

async function loadRisk(project) {
  riskError.value = ''
  try {
    const query = project ? `?project=${encodeURIComponent(project)}` : ''
    riskPayload.value = await get(`/risk${query}`)
  } catch (err) {
    riskPayload.value = null
    riskError.value = err.message
  }
}

watch(
  () => (detail.value ? null : store.project),
  (project) => {
    if (project !== null && project !== riskProject.value) {
      riskProject.value = project
      loadRisk(project)
    }
  },
  { immediate: true },
)

const riskScore = computed(() => {
  const score = riskPayload.value && riskPayload.value.score
  return score && Number.isFinite(Number(score.total))
    ? Number(score.total) : null
})

/** 类型分布：跨镜头摊平的**库里**计数 —— 与左栏「资产规模」同一数据源
 *  （`lenses.kinds`），两个地方各数一遍迟早分叉。 */
const kindSummary = computed(() => {
  const rows = []
  for (const row of store.lensRows) {
    for (const kind of (row.data && row.data.kinds) || []) {
      rows.push({ kind: kind.kind, count: Number(kind.count) || 0 })
    }
  }
  return rows.sort((a, b) => b.count - a.count || a.kind.localeCompare(b.kind))
})

/**
 * 名下资产计数（0.20.0 ·「每个域名名下有什么」）。
 *
 * 只读 `store.domainSummary`（`selectEntity` 点开域名时按项目拉一次，镜头页
 * 下拉同吃这一份）—— **组件自己不发请求**：这里的自动请求会打爆「复制给 AI」
 * 行为测试钉死的 get ×1，而那条断言守的是"复制不附带别的请求"，与名下计数
 * 毫无关系。缓存还没到位时返回 null，模板给一句占位，数据到了自动补上。
 */
const domainAssets = computed(() => {
  const entity = detail.value?.entity
  if (!entity || entity.kind !== 'domain') return null
  const row = (store.domainSummary?.rows || [])
    .find((entry) => entry.domain === entity.entity_key)
  if (!row) return null
  return {
    total: Number(row.total) || 0,
    kinds: Object.entries(row.kinds || {})
      .map(([kind, count]) => ({ kind, count: Number(count) || 0 }))
      .sort((a, b) => b.count - a.count || a.kind.localeCompare(b.kind)),
  }
})
/** 这个实体参与过的、仍然生效的合并事件。**单独一条读**
 *  （`GET /entities/{id}/merges`）而不是塞进 `/entities/{id}` —— 后者的形状
 *  是对外契约，3.8 不动它。 */
const merges = computed(() => store.selectedMerges || [])
const openGroups = ref({ tool: true, manual: true, imported: true })
const newTag = ref('')
const tagError = ref('')
const opError = ref('')
const busy = ref(false)

/** 「复制给 AI」的可见状态：成功反馈与失败提示**都必须落在界面上** ——
 *  剪贴板这种"点了没反应"的操作，静默失败会让人以为复制成功了。 */
const copyBusy = ref(false)
const copyFeedback = ref('')
const copyError = ref('')
/** 剪贴板不可用时展开的上下文包原文：回退不是道歉，是把文本摆出来让人
 *  选中手动复制（选中文本 / 提示手动复制 —— 二选一的下限，这里是两条都给）。 */
const manualMarkdown = ref('')

/** 合并的候选：当前**批量选中**里除了自己以外的那些实体（3.8）。
 *  批量选中是画布上 `Shift` 出来的东西，与"批量改状态"用的是同一个集合 ——
 *  两套选中状态迟早会分叉，而分叉的症状是"合并进去的不是我圈的那些"。 */
const mergeTargets = computed(() => {
  const self = detail.value?.entity?.id
  return (store.selection || []).filter((id) => id !== self)
})

const LIFECYCLE_LABEL = { active: '在用', retired: '已退休' }

async function run(fn, what) {
  if (busy.value) return
  busy.value = true
  opError.value = ''
  try {
    await fn()
  } catch (err) {
    opError.value = `${what}失败：${err.message}`
  } finally {
    busy.value = false
  }
}

function mergeHere() {
  const entity = detail.value?.entity
  if (!entity || !mergeTargets.value.length) return
  // 成员的 key 从画布上的节点取 —— 那是**界面上看得见的那个 key**，
  // 而不是再从别处查一遍（查出来的可能与眼前这张卡片不是同一个东西）。
  const keys = mergeTargets.value.map((id) => {
    const node = store.visible.nodes.find((n) => Number(n.data?.entityId) === Number(id))
    return node ? String(node.data.key) : null
  }).filter(Boolean)
  if (keys.length !== mergeTargets.value.length) {
    opError.value = '有些被选中的实体现在不在画布上（受筛选或载入上限影响）—— ' +
      '合并必须在看得见它们的时候做'
    return
  }
  return run(() => store.mergeEntities({
    survivorId: entity.id, survivorKey: entity.entity_key, memberKeys: keys,
  }), '合并')
}

function unmerge(eventId) {
  return run(() => store.unmergeEntity(detail.value.entity.id, eventId), '拆开')
}

function setLifecycle(entityId, lifecycle) {
  return run(() => store.setLifecycle([entityId], lifecycle), '改状态')
}

const ORIGIN_LABEL = { tool: '工具产出', manual: '人工写入', imported: '导入带入' }

const grouped = computed(() => {
  const buckets = new Map()
  for (const obs of (detail.value?.observations || [])) {
    const key = obs.origin || 'tool'
    if (!buckets.has(key)) buckets.set(key, [])
    buckets.get(key).push(obs)
  }
  return [...buckets.entries()].map(([origin, items]) => ({ origin, items }))
})

const outRelations = computed(() =>
  (detail.value?.relations || []).filter((r) => r.direction === 'out'))
const inRelations = computed(() =>
  (detail.value?.relations || []).filter((r) => r.direction === 'in'))

// ---------------------------------------------------------------------------
// A · 敏感观测值的**呈现层防线**（GPT 评审七）
// ---------------------------------------------------------------------------
//
// 先把事实说清楚：**原文从未入库**。`scan.secret_key` 存的是
// `规则名:sha256(规则名:值)`，观测值落库的唯一出口 `ingest._value_text` 过
// `redact_secrets`（前4位****）。这里防的是**漏网**：未登记形态的凭据万一
// 以原文进了观测，界面默认按 `前4位****` 预览，点「显示」才展开 —— 与后端
// `mask_secret` 同形。所以也**不做假的"显示原文"**：值本来就是掩码/哈希时
// （含 `****`）连"敏感"都不判，没有可遮的东西。
const SENSITIVE_RE = [
  /gh[pousr]_[A-Za-z0-9]{10,}/,                 // GitHub token 家族
  /github_pat_[A-Za-z0-9_]{10,}/,
  /sk-[A-Za-z0-9_-]{10,}/,                      // OpenAI 式密钥
  /xox[baprs]-[A-Za-z0-9-]{10,}/,               // Slack
  /AKIA[0-9A-Z]{16}/,                           // AWS Access Key Id
  /AIza[0-9A-Za-z_-]{30,}/,                     // Google API key
  /-----BEGIN [A-Z ]*PRIVATE KEY-----/,
  /eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\./,  // JWT（三段式头.载荷.签名）
  /\b(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|auth)\s*[=:]\s*\S/i,
]

/** 这个值要不要默认遮蔽。**保守方向**：只认已知凭据形态，不做熵启发式 ——
 *  误伤 location 的 JSON / 时间戳会让"显示"按钮变成常驻噪音。 */
function looksSensitive(value) {
  const text = String(value ?? '')
  if (!text || text.includes('****')) return false   // 已是掩码形态：没有可遮的
  return SENSITIVE_RE.some((re) => re.test(text))
}

/** 与后端 `mask_secret` 同形：前 4 位 + ****。 */
function sensitivePreview(value) {
  return String(value ?? '').slice(0, 4) + '****'
}

//: 展开状态按观测 id 记（同一份原文在两个实体里出现时互不干扰）。
const revealed = ref({})
//: 复制反馈也按观测 id —— 剪贴板操作**成功与失败都必须可见**（与「复制给 AI」
//: 同款纪律：点了没反应会让人以为复制成功了）。
const obsCopy = ref({})

function toggleReveal(obs) {
  revealed.value = { ...revealed.value, [obs.id]: !revealed.value[obs.id] }
}

async function copyObs(obs) {
  const clipboard = typeof navigator === 'undefined' ? null : navigator.clipboard
  if (!clipboard || typeof clipboard.writeText !== 'function') {
    obsCopy.value = { ...obsCopy.value,
                      [obs.id]: { text: '没有剪贴板 API', bad: true } }
    return
  }
  try {
    await clipboard.writeText(String(obs.value))
    obsCopy.value = { ...obsCopy.value, [obs.id]: { text: '已复制', bad: false } }
  } catch (err) {
    obsCopy.value = { ...obsCopy.value,
                      [obs.id]: { text: `复制失败：${err.message || err}`, bad: true } }
  }
}

// ---------------------------------------------------------------------------
// A · 「复制给 AI」二次确认（GPT 评审七："非常严重的问题"）
// ---------------------------------------------------------------------------
//: 只有 `kind === 'secret'` 需要确认 —— 上下文包会带上凭据观测。确认块是
//: **内联**的（不是弹窗）：本机工具里模态框唯一的效果是训练人无脑点"确定"。
//: `copyForAI` 内部一字未改（verify_g5 B4a 钉着它的端点与剪贴板形态）。
const confirmCopy = ref(false)
const needsConfirm = computed(() => detail.value?.entity?.kind === 'secret')

function onCopyClick() {
  if (needsConfirm.value && !confirmCopy.value) {
    confirmCopy.value = true
    return
  }
  copyForAI()
}

// ---------------------------------------------------------------------------
// C · 风险路径（GPT 评审九："这个 JWT 到底关联到了什么"）
// ---------------------------------------------------------------------------
//
// 一次显式点击换**一个请求**：`GET /graph/{id}/neighbors?depth=5&limit=500`
// （服务端 BFS 裁好的子图），客户端在它上面再走一次无向 BFS 找目标 ——
// 不改后端、不加端点、不碰图（结果只是几枚可点的 chip）。
//
// 为什么按钮触发而不自动：右栏选中态的 `get` 调用被「复制给 AI」行为测试
// 钉死为 1（那条断言守的是"复制不附带别的请求"）—— 自动拉邻居会把它打红，
// 而它红的原因与复制毫无关系。更重要的理由是**代价**：depth=5 在大库上不是
// 免费的，不该为每个被点开的实体都付一次。
//
// 目标随起点反转：一般实体 → 走到 `domain`（攻击面链路的顶层）；域名 →
// 走到最近的 `secret`/`vuln`（它的证据侧）。两边答的是同一个问题：
// **这个东西沿着关系连到了什么**。
const pathState = ref('idle')     // idle | busy | done | none
const pathError = ref('')
const pathNodes = ref([])         // [{id, kind, key, rel}] —— 链，首项是起点

function pathGoalKinds(entity) {
  return entity.kind === 'domain' ? ['secret', 'vuln'] : ['domain']
}

function pathGoalText(entity) {
  return entity.kind === 'domain'
    ? '从这个域名沿关系走到最近的 secret / vuln（它的证据侧）'
    : '从这个实体沿关系走到域名（攻击面链路的顶层）'
}

async function computeRiskPath() {
  const entity = detail.value?.entity
  if (!entity || pathState.value === 'busy') return
  pathState.value = 'busy'
  pathError.value = ''
  pathNodes.value = []
  try {
    const project = store.project
      ? `&project=${encodeURIComponent(store.project)}` : ''
    const payload = await get(
      `/graph/${entity.id}/neighbors?depth=5&limit=500${project}`)
    const nodes = payload?.nodes || []
    const edges = payload?.edges || []
    const byId = new Map(nodes.map((n) => [String(n.id), n]))
    const adj = new Map()
    for (const edge of edges) {
      const src = String(edge.src)
      const dst = String(edge.dst)
      if (!adj.has(src)) adj.set(src, [])
      if (!adj.has(dst)) adj.set(dst, [])
      adj.get(src).push([dst, edge])
      adj.get(dst).push([src, edge])
    }
    const start = String(entity.id)
    const goals = new Set(pathGoalKinds(entity))
    const seen = new Set([start])
    const parent = new Map()          // id → { from, edge }
    let frontier = [start]
    let goal = null
    // 逐层 BFS：同一层里先找目标，找不到才展开下一层 —— 链在所有最短路径里
    // 取第一条（确定性：邻接按边序），不会"每次点都换一条"。
    while (frontier.length && !goal) {
      const next = []
      for (const cur of frontier) {
        for (const [nid, edge] of adj.get(cur) || []) {
          if (seen.has(nid)) continue
          seen.add(nid)
          parent.set(nid, { from: cur, edge })
          const node = byId.get(nid)
          if (node && goals.has(String(node.kind))) { goal = nid; break }
          next.push(nid)
        }
        if (goal) break
      }
      frontier = next
    }
    if (!goal) {
      pathState.value = 'none'
      return
    }
    const chain = []
    let cur = goal
    while (cur !== start && parent.has(cur)) {
      const { from, edge } = parent.get(cur)
      const node = byId.get(cur)
      chain.unshift({
        id: node ? node.id : Number(cur),
        kind: node ? String(node.kind || '') : '',
        key: node ? String(node.entity_key || `#${cur}`) : `#${cur}`,
        rel: String(edge.rel || ''),
      })
      cur = from
    }
    pathNodes.value = [{
      id: entity.id, kind: String(entity.kind || ''),
      key: String(entity.entity_key || ''), rel: '',
    }, ...chain]
    pathState.value = 'done'
  } catch (err) {
    pathError.value = `风险路径计算失败：${err.message}`
    pathState.value = 'idle'
  }
}

function toggleGroup(origin) {
  openGroups.value[origin] = !openGroups.value[origin]
}

async function addTag() {
  const tag = newTag.value.trim()
  if (!tag) return
  tagError.value = ''
  try {
    await store.setEntityTags(detail.value.entity.id, { add: [tag] })
    newTag.value = ''
  } catch (err) {
    tagError.value = err.message
  }
}

/** 删一个标签。**删除也留审计**（v3.2 §6.1）—— `tag_events` 上那条 remove
 *  行与 `activity_log` 的 `tag.remove` 都在数据层写，界面这一侧什么都不用做。 */
async function removeTag(tag) {
  tagError.value = ''
  try {
    await store.setEntityTags(detail.value.entity.id, { remove: [tag] })
  } catch (err) {
    tagError.value = err.message
  }
}

/**
 * 「复制给 AI」：取上下文包（`GET /agent/context`），把 **Markdown** 放进本机剪贴板。
 *
 * 三条边界，每一条都是这条功能最容易做错的地方：
 *
 *   1. **只复制，不发送** —— 请求走同源相对路径（`api.js` 的 `get`），
 *      剪贴板是本机操作。没有任何"发给某个 AI 服务"的一步：那会撞 R7-ASSETS，
 *      也违背这个工具零出站的前提。
 *   2. **深度 1 是端点的默认** —— 界面不自己发明一个更大的 depth 再让上下文包
 *      与详情页给出两个口径的邻居数。
 *   3. **剪贴板是增强，不是前提** —— 非安全上下文 / 权限被拒 / 老浏览器里
 *      `navigator.clipboard` 不在或 `writeText` 抛错。三种情况都把原文展开在
 *      界面上让人手动复制，而不是"点了没反应"。
 */
async function copyForAI() {
  const entity = detail.value?.entity
  if (!entity || copyBusy.value) return
  copyBusy.value = true
  copyFeedback.value = ''
  copyError.value = ''
  manualMarkdown.value = ''
  let markdown = ''
  try {
    const data = await get(`/agent/context?entity_id=${entity.id}&depth=1`)
    markdown = String((data && data.markdown) || '')
    if (!markdown) throw new Error('返回里没有 markdown')
  } catch (err) {
    copyError.value = `取上下文失败：${err.message}`
    copyBusy.value = false
    return
  }
  const clipboard = typeof navigator === 'undefined' ? null : navigator.clipboard
  if (clipboard && typeof clipboard.writeText === 'function') {
    try {
      await clipboard.writeText(markdown)
      copyFeedback.value = `已复制 ${markdown.length} 字`
    } catch (err) {
      manualMarkdown.value = markdown
      copyError.value = `剪贴板写入失败（${err.message || err}）—— 已展开原文，请手动复制`
    }
  } else {
    manualMarkdown.value = markdown
    copyError.value = '这个环境没有剪贴板 API —— 已展开原文，请手动复制'
  }
  copyBusy.value = false
}

/** 手动回退里的全选：点一下文本就整段选中，`Ctrl/Cmd+C` 直接走。 */
function selectManual(event) {
  event.target.select()
}

/**
 * 关系跳转（0.20.0 修"坏链"）。旧版只在**已载入的窗口**里找 key，找不到就
 * 一句"画布上找不到"了事 —— 内容多的库（limit=500）里关系对面几乎总在窗外。
 * 现在走 `store.ensureVisible`：按 `other_id` 拉邻居合入再选中，详情永远打开。
 */
function goTo(rel) {
  store.ensureVisible(rel.other_id)
}

//: 换实体 = 换上下文：确认块 / 展开态 / 复制反馈 / 风险链全部归零。不重置的
//: 话，上一个 secret 的"已确认"会让下一个 secret 一击即发 —— 而那正是确认
//: 要防的事。
watch(detail, () => {
  confirmCopy.value = false
  revealed.value = {}
  obsCopy.value = {}
  pathState.value = 'idle'
  pathError.value = ''
  pathNodes.value = []
})
</script>

<template>
  <aside class="right">
    <!-- 默认态 = 项目概览（GPT ④⑫）：原来两行说明文字占着一整个右栏。风险分
         懒加载（见 loadRisk），类型分布与左栏同源；「点一个实体看详情」降级
         为底部脚注，不再冒充正文。 -->
    <template v-if="!detail">
      <section>
        <h2 class="section">项目概览</h2>
        <div class="stats">
          <div class="stat">
            <b>{{ fmtInt(store.projectTotal) }}</b>
            <span>实体（库中）</span>
          </div>
          <div class="stat">
            <b :class="{ faint: riskScore === null }">
              {{ riskScore === null ? (riskError ? '—' : '…') : fmtInt(riskScore) }}
            </b>
            <span>风险分</span>
          </div>
          <div class="stat">
            <b>{{ fmtInt(store.openInboxCount) }}</b>
            <span>待裁决</span>
          </div>
        </div>
        <p v-if="riskError" class="bad small">风险分拉取失败：{{ riskError }}</p>
        <p class="faint small">
          风险分与「风险」页同一份口径（`GET /risk`）—— 界面不自算。
        </p>
      </section>

      <section>
        <h2 class="section">类型分布</h2>
        <ul class="dist">
          <li v-for="row in kindSummary" :key="'d' + row.kind">
            <span class="swatch" :style="{ background: kindColor(row.kind) }" />
            <span class="mono name">{{ row.kind }}</span>
            <span class="num">{{ fmtInt(row.count) }}</span>
          </li>
        </ul>
        <p v-if="!kindSummary.length" class="dim small">（还没有登记的类型）</p>
      </section>

      <p class="faint small hint">
        点画布上的一个实体，这里显示它的基本信息 / 来源观测 / 风险路径 /
        关联资产 / AI分析。聚合视图下（节点数超过 200）<b>点一张类型卡即可
        下钻</b>到逐个实体。
      </p>
    </template>

    <template v-else>
      <!-- 基本信息头（评审八）：key + 类型 + 状态。secret 额外挂 🔒 已掩码 ——
           身份是哈希、观测值入库前已掩码（原文从未入库），把这件事说在最显眼处。 -->
      <header class="head">
        <div class="title">
          <span class="swatch" :style="{ background: kindColor(detail.entity.kind) }" />
          <span class="mono key">{{ detail.entity.entity_key }}</span>
        </div>
        <div class="tags">
          <span class="chip accent">{{ detail.entity.kind }}</span>
          <span v-if="detail.entity.kind === 'secret'" class="chip warn"
                title="身份 = 规则名:哈希；观测值入库前已掩码，原文从未入库">
            🔒 已掩码
          </span>
          <span class="chip">{{ detail.entity.lifecycle }}</span>
          <span class="chip mono">#{{ detail.entity.id }}</span>
        </div>
      </header>

      <!-- 基本信息（评审八的骨架第一格）：出现时间 + 人工保护 + 状态。
           「状态」按钮从原来的顶级区并进来 —— 详情页的主干是
           基本信息 / 来源 / 风险 / 关联资产 / AI分析，"状态"不值一个孤零零的
           顶级区（它就是基本信息的一个字段）。 -->
      <section>
        <h2 class="section">基本信息</h2>
        <dl class="kv">
          <dt>首次出现</dt><dd class="mono">{{ detail.entity.first_seen || '—' }}</dd>
          <dt>最近出现</dt><dd class="mono">{{ detail.entity.last_seen || '—' }}</dd>
          <dt>人工保护</dt>
          <dd class="mono">
            {{ detail.manual.protected_fields.length
              ? detail.manual.protected_fields.join(', ') : '（无）' }}
          </dd>
        </dl>
        <div class="acts lifecycle">
          <button v-for="value in ['active', 'retired']" :key="value" class="ghost"
                  :class="{ on: detail.entity.lifecycle === value }"
                  :disabled="busy || detail.entity.lifecycle === value"
                  @click="setLifecycle(detail.entity.id, value)">
            {{ LIFECYCLE_LABEL[value] }}
          </button>
        </div>
        <p class="faint small">
          改状态是**一个 job**（一次事务，要么全成要么全不成），撤销按钮就在
          作业历史里 —— 与其它写操作是同一个。
        </p>
      </section>

      <section>
        <h2 class="section">观测（{{ fmtInt(detail.observations.length) }}）</h2>
        <p v-if="!detail.observations.length" class="warn small">
          一条观测都没有 —— 这是一个**没测过**的实体。
        </p>
        <div v-for="group in grouped" :key="group.origin" class="group">
          <button class="group-head ghost" @click="toggleGroup(group.origin)">
            <span class="caret">{{ openGroups[group.origin] ? '▾' : '▸' }}</span>
            {{ ORIGIN_LABEL[group.origin] || group.origin }}
            <span class="badge">{{ group.items.length }}</span>
          </button>
          <ul v-show="openGroups[group.origin]" class="obs">
            <!-- 敏感值默认遮蔽（评审七）：命中已知凭据形态的值先给
                 `前4位****` 预览，「显示」才展开；「复制」始终可用，且成功/
                 失败都有可见反馈（剪贴板操作不许"点了没反应"）。 -->
            <li v-for="obs in group.items" :key="obs.id">
              <span class="field mono">{{ obs.semantic_field }}</span>
              <template v-if="looksSensitive(obs.value)">
                <span v-if="!revealed[obs.id]" class="value mono masked">
                  {{ sensitivePreview(obs.value) }}
                </span>
                <span v-else class="value mono">{{ obs.value }}</span>
                <span class="obs-acts">
                  <button class="ghost xs" @click="toggleReveal(obs)">
                    {{ revealed[obs.id] ? '隐藏' : '显示' }}
                  </button>
                  <button class="ghost xs" @click="copyObs(obs)">复制</button>
                </span>
                <span v-if="obsCopy[obs.id]" class="chip"
                      :class="obsCopy[obs.id].bad ? 'warn' : 'ok'">
                  {{ obsCopy[obs.id].text }}
                </span>
              </template>
              <template v-else>
                <span class="value mono">{{ obs.value }}</span>
              </template>
              <span class="chip" :class="{
                ok: obs.confidence === 'high',
                warn: obs.confidence === 'low' || obs.confidence === 'conflict',
              }">{{ obs.confidence }}</span>
            </li>
          </ul>
        </div>
        <!-- secret 的脚注（评审七）：把"这里没有原文"说清楚 —— 而不是让人
             以为原文被藏起来了。 -->
        <p v-if="detail.entity.kind === 'secret'" class="faint small">
          🔒 这些观测值是入库前掩码后的形态（前4位+****）；身份是
          规则名:哈希 —— <b>原文从未入库</b>，没有「显示原文」这回事。
        </p>
      </section>

      <!-- 来源的第二半：时间线（评审八的「时间」格）—— 紧跟观测，它们说的是
           同一件事（这个值是什么时候、由谁记下来的）。 -->
      <section>
        <h2 class="section">时间线</h2>
        <ul class="timeline">
          <li v-for="row in detail.timeline" :key="row.id">
            <span class="mono at">{{ row.at }}</span>
            <span class="mono action">{{ row.action }}</span>
            <span class="faint">{{ row.actor }}</span>
          </li>
          <li v-if="!detail.timeline.length" class="dim">（无）</li>
        </ul>
      </section>

      <!-- 风险路径（评审九）：一键沿关系走子图 —— Secret → … → Domain。
           显式按钮触发（理由见 computeRiskPath 的注释），链上的每一段都可点。 -->
      <section>
        <h2 class="section">风险路径</h2>
        <p class="faint small">{{ pathGoalText(detail.entity) }}</p>
        <div class="acts">
          <button class="ghost path-btn" :disabled="pathState === 'busy'"
                  @click="computeRiskPath">
            {{ pathState === 'busy' ? '正在沿关系搜索…' : '计算风险路径' }}
          </button>
          <button v-if="pathState === 'done' || pathState === 'none'"
                  class="ghost" @click="pathState = 'idle'; pathNodes = []">
            清除
          </button>
        </div>
        <p v-if="pathError" class="bad small">{{ pathError }}</p>
        <p v-if="pathState === 'none'" class="dim small">
          5 跳邻居里没有走到目标 —— 关系数据还不够连出一条链（多导一份
          httpx / nuclei 输出会更连通）。
        </p>
        <div v-if="pathState === 'done' && pathNodes.length" class="chain">
          <template v-for="(hop, i) in pathNodes" :key="'hop' + hop.id">
            <!-- 段标 = **进入这一跳**的那条边（重建时 hop.rel 就存的它）——
                 写成 i-1 会把上一段的标签挪到这一段，最后一跳的关系永远不出
                 现（riskpath.test 钉着 found_on/resolves_to 两条都在）。 -->
            <span v-if="i" class="chain-rel mono">{{ pathNodes[i].rel || '→' }}
              <span class="arrow">↓</span>
            </span>
            <button class="chain-node" :style="{ '--band': kindColor(hop.kind) }"
                    :title="`${hop.kind} · 点击在画布上打开`"
                    @click="store.ensureVisible(hop.id)">
              <span class="mono ck">{{ hop.key }}</span>
              <span class="ckind">{{ hop.kind }}</span>
            </button>
          </template>
        </div>
      </section>

      <!-- 域名视图入口（0.19.0）。**互斥的单按钮**：不在视图里是「只看它的
           资产」，正在看它就是「退出域名视图」—— 两个按钮并排会让人先问
           "我现在在哪"，而一个状态只需要一个动作。看别的域名时按钮回到
           「只看它的资产」，点了直接换视图（setDomainFocus 是覆盖不是叠加）。
           归属它放在「关联资产」名下（评审八）：它回答的正是"这个域名名下
           有哪些资产"。 -->
      <section v-if="detail.entity.kind === 'domain'">
        <h2 class="section">域名视图</h2>
        <div class="acts">
          <button v-if="store.filter.underDomain !== detail.entity.entity_key"
                  class="primary" @click="store.setDomainFocus(detail.entity.entity_key)">
            只看它的资产
          </button>
          <button v-else class="primary" @click="store.clearDomainFocus()">
            退出域名视图
          </button>
        </div>

        <!-- 名下资产（0.20.0 ·「每个域名名下有什么」）：计数与镜头页下拉的
             `key (N)`、`with_domain` 分段行**同源**（store.domainSummary，分组
             取首选的口径）—— 三处显示的 N 必须是同一个数，所以这里不再自己数
             一遍。选中态一个请求都不发（见 domainAssets 的注释）。 -->
        <ul v-if="domainAssets" class="dist">
          <li v-for="row in domainAssets.kinds" :key="row.kind">
            <span class="swatch" :style="{ background: kindColor(row.kind) }" />
            <span class="mono name">{{ row.kind }}</span>
            <span class="num">{{ fmtInt(row.count) }}</span>
          </li>
        </ul>
        <p v-if="domainAssets" class="faint small">
          名下共 {{ fmtInt(domainAssets.total) }} 个实体（含它自己）。
        </p>
        <p v-else class="faint small">（名下计数未载入）</p>
        <p class="faint small">
          归属是推导的（域名后缀 + 解析反查 + 漏洞落点），不是一条存着的边。
        </p>
      </section>

      <section>
        <h2 class="section">关联资产（出 {{ fmtInt(outRelations.length) }} / 入 {{ fmtInt(inRelations.length) }}）</h2>
        <ul class="rels">
          <li v-for="rel in outRelations" :key="'o' + rel.id">
            <span class="arrow ok">→</span>
            <span class="mono verb">{{ rel.rel }}</span>
            <a class="mono other" @click="goTo(rel)">{{ rel.other_key }}</a>
          </li>
          <li v-for="rel in inRelations" :key="'i' + rel.id">
            <span class="arrow dim">←</span>
            <span class="mono verb">{{ rel.rel }}</span>
            <a class="mono other" @click="goTo(rel)">{{ rel.other_key }}</a>
          </li>
          <li v-if="!detail.relations.length" class="dim">（无）</li>
        </ul>
      </section>

      <section>
        <h2 class="section">标签</h2>
        <div class="tags">
          <span v-for="tag in detail.tags" :key="tag" class="chip removable">
            {{ tag }}
            <button class="x" title="删掉这个标签（删除也留审计）"
                    @click="removeTag(tag)">×</button>
          </span>
          <span v-if="!detail.tags.length" class="faint small">（无）</span>
        </div>
        <div class="addtag">
          <input v-model="newTag" list="tag-vocab" placeholder="加一个标签（标注）"
                 @keydown.enter="addTag" />
          <!-- 词表来自 `GET /tags`（**库里的**数），不是"界面上见过的那些"：
               一个标签从所有实体上摘掉之后，它下一次就只能靠人记得重新敲。 -->
          <datalist id="tag-vocab">
            <option v-for="row in store.tags" :key="row.tag" :value="row.tag">
              {{ row.count }} 个实体
            </option>
          </datalist>
          <button class="ghost" @click="addTag">加</button>
        </div>
        <p v-if="tagError" class="bad small">{{ tagError }}</p>
        <p class="faint small">
          只能改标签与人工保护标记。**人工写入字段的值**走 2.6 的写路径，
          目前 `PATCH /entities/{id}` 对它返回 501。
        </p>
      </section>

      <section>
        <h2 class="section">合并（{{ merges.length }}）</h2>
        <ul class="merges">
          <li v-for="event in merges" :key="event.event_id">
            <span class="mono">#{{ event.event_id }}</span>
            <span class="mono">← {{ (event.member_keys || []).join(' / ') }}</span>
            <button class="ghost" :disabled="busy" @click="unmerge(event.event_id)">
              拆开
            </button>
          </li>
          <li v-if="!merges.length" class="dim">（没合并过）</li>
        </ul>
        <div class="acts">
          <button class="ghost" :disabled="busy || !mergeTargets.length"
                  :title="mergeTargets.length ? '' : '先在画布上按住 Shift 选中要并入的卡片'"
                  @click="mergeHere">
            把选中的 {{ mergeTargets.length }} 个并入这里
          </button>
        </div>
        <p class="faint small">
          合并是**身份覆盖**：事实一条都不移动，所以拆开天然精确可逆。它走
          `ops.merge` / `ops.unmerge` —— 在界面里拼一个"把两张卡片画成一张"
          会同时丢掉可逆性与冲突登记，而症状要等到有人想拆开时才出现。
        </p>
      </section>
      <p v-if="opError" class="bad small">{{ opError }}</p>

      <!-- AI分析（评审八的尾格）：「复制给 AI」是这一格今天唯一真的动作 ——
           取上下文包进本机剪贴板。secret 实体先出确认块（评审七），确认后
           才走 `copyForAI`（其内部形态被 verify_g5 B4a 钉死，一字未改）。 -->
      <section>
        <h2 class="section">AI分析</h2>
        <div class="acts">
          <button class="ghost copy-ai" :disabled="copyBusy" @click="onCopyClick">
            {{ copyBusy ? '正在取上下文…' : '复制给 AI' }}
          </button>
          <span v-if="copyFeedback" class="chip ok">{{ copyFeedback }}</span>
        </div>

        <!-- 敏感实体的二次确认（评审七）：内联而不是弹窗 —— 本机工具里模态框
             唯一的效果是训练人无脑点"确定"。 -->
        <div v-if="needsConfirm && confirmCopy && !copyBusy && !copyFeedback"
             class="confirm panel">
          <p class="warn small">
            <b>即将把敏感凭据放进剪贴板</b> —— 这个实体是 secret，上下文包里
            可能包含：
          </p>
          <ul class="confirm-list">
            <li>访问令牌 / 会话凭据</li>
            <li>身份凭据（观测值已掩码，但规则与位置会带上）</li>
            <li>关联到它的接口与主机</li>
          </ul>
          <div class="acts">
            <button class="ghost" @click="confirmCopy = false">取消</button>
            <button class="primary copy-ai-confirm" @click="copyForAI">
              确认复制
            </button>
          </div>
          <p class="faint small">
            复制是**本机剪贴板**操作，不向任何地方发送。
          </p>
        </div>

        <p v-if="copyError" class="bad small">{{ copyError }}</p>
        <template v-if="manualMarkdown">
          <p class="warn small">
            下面是上下文包原文 —— 点一下选中，Ctrl/Cmd+C 手动复制。
          </p>
          <textarea class="manual mono" readonly rows="8" :value="manualMarkdown"
                    @click="selectManual"></textarea>
        </template>
        <p class="faint small">
          只把 Markdown 放进本机剪贴板（`navigator.clipboard`），不向任何地方发送。
        </p>
      </section>
    </template>
  </aside>
</template>

<style scoped>
.right {
  width: 330px;
  flex: none;
  border-left: 1px solid var(--line);
  background: var(--panel);
  /* 12 → 16：右栏原先贴边，内容与边框黏在一起（视觉整改的"呼吸感"）。 */
  padding: var(--sp-4);
  overflow-y: auto;
}
.pad { padding: 8px; }
.small { font-size: 11px; line-height: 1.5; }   /* 脚注小字，刻意比正文低一档 */

/* 项目概览（GPT ④⑫）：三个大数横排 + 类型分布行。 */
.stats { display: flex; gap: var(--sp-4); }
.stat { display: flex; flex-direction: column; gap: 2px; }
.stat b {
  font-size: 20px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  line-height: 1.1;
}
.stat b.faint { color: var(--faint); }
.stat span { font-size: 11px; color: var(--faint); }
.dist { list-style: none; margin: 0; padding: 0; font-size: var(--fs-md); }
.dist li {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 3px 0;
}
.dist .name { flex: 1; overflow: hidden; text-overflow: ellipsis; }
.dist .num {
  font-variant-numeric: tabular-nums;
  color: var(--dim);
  font-size: 11px;
}
/* 脚注：原默认态的两行说明降级到这里，顶部再让一条淡线与正文隔开。 */
.hint {
  margin-top: var(--sp-4);
  padding-top: var(--sp-3);
  border-top: 1px solid var(--line);
}
section + section { margin-top: 18px; }
/* 标签与正文的层级 = 11px 大写标签 + 拉开的底距，而不是"两行差不多大的字"。 */
.right h2.section { margin-bottom: var(--sp-3); }
.head { margin-bottom: var(--sp-3); }
.title { display: flex; align-items: center; gap: 7px; margin-bottom: 6px; }
.key { font-size: 13px; word-break: break-all; }
.swatch { width: 9px; height: 9px; border-radius: 2px; flex: none; }
.tags { display: flex; flex-wrap: wrap; gap: 5px; }
/* 正文 11 → 12（--fs-md）：11px 在 330px 栏里读着费劲；脚注（.small）仍是
   11px —— 层级靠"正文大一档、脚注小一档"拉开，不是全栏一个字号。 */
.kv { display: grid; grid-template-columns: 84px 1fr; gap: 3px 8px; margin: 0;
      font-size: var(--fs-md); }
.kv dt { color: var(--faint); }
.kv dd { margin: 0; word-break: break-all; }
.group { margin-bottom: 4px; }
.group-head { width: 100%; text-align: left; display: flex; align-items: center;
              gap: 6px; padding: 3px 4px; color: var(--dim); }
.caret { width: 9px; }
.obs { list-style: none; margin: 2px 0 6px; padding: 0 0 0 14px; }
.obs li { display: flex; align-items: center; gap: 6px; padding: 3px 0;
          font-size: var(--fs-md); }
.obs .field { color: var(--faint); min-width: 92px; }
.obs .value { flex: 1; word-break: break-all; }
.rels { list-style: none; margin: 0; padding: 0; font-size: var(--fs-md); }
.rels li { display: flex; align-items: center; gap: 6px; padding: 3px 0; }
.rels .verb { color: var(--dim); min-width: 74px; }
.rels .other { color: var(--accent); cursor: pointer; word-break: break-all; }
.rels .other:hover { text-decoration: underline; }
.addtag { display: flex; gap: 6px; margin-top: 8px; }
.addtag input { flex: 1; }
.chip.removable { display: inline-flex; align-items: center; gap: 3px; padding-right: 2px; }
.chip .x {
  border: 0; background: transparent; color: var(--faint); cursor: pointer;
  font-size: 12px; line-height: 1; padding: 0 3px;
}
.chip .x:hover { color: var(--bad, #F03E3E); }
.acts { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.acts button.on { border-color: var(--accent); color: var(--accent); }
.manual {
  width: 100%; margin-top: 6px; font-size: 11px; line-height: 1.5;
  padding: 6px; border: 1px solid var(--line); border-radius: 4px;
  background: var(--bg, #fff); color: var(--dim); resize: vertical;
}
.merges { list-style: none; margin: 0 0 8px; padding: 0; font-size: var(--fs-md); }
.merges li { display: flex; align-items: center; gap: 7px; padding: 3px 0; }
.merges li span { color: var(--dim); word-break: break-all; }
.timeline { list-style: none; margin: 0; padding: 0; font-size: var(--fs-md); }
.timeline li { display: flex; gap: 7px; padding: 2px 0; }
.timeline .at { color: var(--faint); font-size: 10px; }
.timeline .action { color: var(--dim); }

/* --- 敏感观测值（评审七）：预览 + 显示/复制两枚小按钮。 ------------------ */
.lifecycle { margin-top: 8px; }
.obs .masked { color: var(--warn); letter-spacing: .06em; }
.obs-acts { display: inline-flex; gap: 3px; flex: none; }
.obs-acts .xs { padding: 1px 6px; font-size: 10px; line-height: 15px; }

/* --- 敏感实体的内联确认块（评审七）。 ------------------------------------ */
.confirm {
  margin-top: 8px;
  padding: 10px 12px;
  border: 1px solid var(--warn);
  border-radius: var(--radius-card);
  background: var(--panel-2);
}
.confirm p { margin: 0 0 6px; }
.confirm-list {
  margin: 0 0 8px;
  padding-left: 18px;
  font-size: 11px;
  line-height: 1.7;
  color: var(--dim);
}

/* --- 风险路径链（评审九）。 ---------------------------------------------- */
.chain {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px;
  margin-top: 8px;
}
.chain-rel { font-size: 10px; color: var(--faint); }
.chain-rel .arrow { color: var(--dim); }
.chain-node {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 1px;
  max-width: 100%;
  padding: 4px 8px 4px 7px;
  border: 1px solid var(--line-card, var(--line));
  border-left: 3px solid var(--band, var(--accent));
  border-radius: var(--radius-ui);
  background: var(--panel-2);
  cursor: pointer;
}
.chain-node:hover { border-color: var(--accent); }
.chain-node .ck {
  font-size: 11px;
  color: var(--fg, inherit);
  max-width: 190px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chain-node .ckind { font-size: 9px; color: var(--faint); }
</style>
