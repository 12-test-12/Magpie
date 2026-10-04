<script setup>
/**
 * 镜头页（计划书 §7.4）。有哪几个镜头**不在这里写死**：左侧导航的清单由
 * `store.lensRows` 从 `kind_registry.lens` 推导（3.2 起如此），这一页只是渲染
 * 当前这一个。
 *
 * 镜头不是"另一张表"：它是同一份数据按 `kind_registry.lens` 切出来的视图
 * （§3.5）。所以这里不新增任何数据源 —— kind 列表来自 `GET /lenses/{lens}`，
 * 每个 kind 的行来自 `GET /entities?kind=…`。
 *
 * **过滤走 SQL**（§7.5 的"排序过滤在 SQL"）：逐个 kind 请求，而不是把全库拉
 * 回来在 JS 里筛。差别在数据量上：一个 10 万实体的库，前者是几条索引查询，
 * 后者是把 10 万行搬到浏览器里。**已知边界**：每个 kind 上限 500 条
 * （`/entities` 的 `MAX_PAGE`），超出的部分本页看不到 —— 分页控件属于后续
 * 步骤，这里把这件事写在页脚，而不是假装列表是完整的。
 *
 * 按 `severity` / `state` 筛（3.2 的漏洞镜头）
 * --------------------------------------------
 * 这两维是 `vuln` 实体上的**观测**，而 `GET /entities` 只回实体行、不回观测
 * —— 所以本页在**已载入的那一页**上按需补齐它们：走的就是详情面板用的同一个
 * `GET /entities/{id}`，**不是**一条漏洞专用接口（那正是 §3.5"GUI 与 CLI 共用
 * 同一实现"要避免的第二条路）。
 *
 * 代价与边界都写在这里，不藏着：
 *   * 只有 `vuln` 镜头会做这件事（`FACETS`），别的镜头一次多余请求都没有；
 *   * 补齐只针对**已经载入的行**（≤ 500，且 8 路并发），结果按 id 缓存，
 *     切筛选/重排不会重取；
 *   * 服务端筛选是 3.5 的 `core/query.py` 的事 —— 那时这一整段会换成一次
 *     `GET /entities?severity=…`，本文件的这部分代码随之删掉。
 */
import { computed, onMounted, ref, watch } from 'vue'
import VirtualTable from '../components/VirtualTable.vue'
import EmptyState from '../components/EmptyState.vue'
import { get } from '../api.js'
import { store } from '../store.js'
import { kindColor } from '../graph.js'

const props = defineProps({
  lens: { type: String, required: true },
  label: { type: String, default: '' },
})

/** 空态上的「立即导入数据」冒给外壳开导入对话框（与画布引导卡同一条路）。 */
const emit = defineEmits(['import'])

const PAGE = 500

/**
 * 哪些镜头需要"从观测补一列"，以及补哪几列。
 *
 * 写成一张表而不是 `if (lens === 'vuln')`：下一个有枚举字段的 kind（3.3 的
 * endpoint 方法、3.4 的接口类型）加一行就够了，而每种来源各写一段"取详情、
 * 抽字段、建下拉"的重复代码正是会分叉的地方。
 */
const FACETS = { vuln: ['severity', 'state'] }

/** 补齐时的并发上限。逐个串行会把 500 行变成肉眼可见的等待，全并发又会把
 *  单写队列的邻居饿着 —— 8 路在本地回环上是"感觉不到"与"不添乱"之间。 */
const ENRICH_CONCURRENCY = 8

const rows = ref([])
const loading = ref(false)
const error = ref('')
const selected = ref(null)
const truncated = ref([])
/** id → {severity, state}。跨刷新保留：同一个实体的观测不会因为换了筛选而变。 */
const facets = ref({})
const enriching = ref(false)
const facetFilter = ref({ severity: '', state: '' })
//: 域名筛选（'' = 全部）。选中时 kind 查询带 `under_domain=`（服务端裁剪），
//: 全部时带 `with_domain=1` 让行带上归属 —— 分段与筛选共用同一套推导原语，
//: 所以"分段看到的"与"筛出来的"不会分叉（后端单真相测试钉住）。
const domainChoice = ref('')
const domainOptions = computed(
  () => (store.domains?.rows || []).map((row) => row.entity_key).sort())

const COLUMNS = [
  { key: 'entity_key', label: '实体', mono: true },
  { key: 'kind', label: '类型', width: '86px' },
  { key: 'observation_count', label: '观测', mono: true, width: '60px' },
  { key: 'out_degree', label: '出边', mono: true, width: '60px' },
  { key: 'first_seen', label: '首次', mono: true, width: '170px' },
  { key: 'last_seen', label: '最近', mono: true, width: '170px' },
]

const facetFields = computed(() => FACETS[props.lens] || [])
const kinds = computed(() => (store.lenses[props.lens]?.kinds || []))
const lockedKinds = computed(() => kinds.value.map((entry) => entry.kind))

/** 下拉里的选项来自**这一页真的出现过的值**，不是一份代码里写死的清单 ——
 *  一个筛不出任何行的选项比没有这个选项更让人困惑。 */
function options(field) {
  const seen = new Set()
  for (const kind of lockedKinds.value) {
    for (const row of rows.value) {
      if (row.kind !== kind) continue
      const value = (facets.value[row.id] || {})[field]
      if (value) seen.add(value)
    }
  }
  return [...seen].sort()
}

function matchesFacets(row) {
  const found = facets.value[row.id] || {}
  return facetFields.value.every(
    (field) => !facetFilter.value[field] || found[field] === facetFilter.value[field])
}

const visibleRows = computed(() => rows.value.filter(matchesFacets))

function setFacet(field, value) {
  facetFilter.value = Object.assign({}, facetFilter.value, { [field]: value })
}

async function load() {
  loading.value = true
  error.value = ''
  selected.value = null
  facetFilter.value = { severity: '', state: '' }
  const collected = []
  const capped = []
  const project = store.project ? `&project=${encodeURIComponent(store.project)}` : ''
  // 两个都是**服务端**参数：under_domain 裁剪（limit=500 本地筛看不见第 501
  // 个）、with_domain 补归属字段（呈现参数，不筛）。选了域名就不必再要归属
  // 字段 —— 整页就是一个分段，分段行反而是一句废话。
  const domain = domainChoice.value
    ? `&under_domain=${encodeURIComponent(domainChoice.value)}`
    : '&with_domain=1'
  try {
    for (const entry of kinds.value) {
      const data = await get(
        `/entities?kind=${encodeURIComponent(entry.kind)}&limit=${PAGE}`
        + `${project}${domain}`)
      collected.push(...data)
      if (data.length >= PAGE) capped.push(entry.kind)
    }
    const seen = new Set()
    rows.value = collected
      .filter((row) => (seen.has(row.id) ? false : seen.add(row.id)))
      .sort((a, b) => a.id - b.id)
      .map((row) => Object.assign({}, row, { __key: row.id }))
    truncated.value = capped
  } catch (err) {
    error.value = err.message
    rows.value = []
  } finally {
    loading.value = false
  }
  await enrich()
}

/** 给需要补列的镜头取回观测（见文件头的"代价与边界"）。失败静默降级：
 *  补不到的那一列留空、筛选里不出现那个值 —— 比整页报错好，因为**列表本身
 *  是好的**，缺的只是一列。 */
async function enrich() {
  if (!facetFields.value.length || !rows.value.length) return
  const pending = rows.value.filter((row) => !(row.id in facets.value))
  if (!pending.length) return
  enriching.value = true
  const queue = pending.slice()
  const run = async () => {
    while (queue.length) {
      const row = queue.shift()
      try {
        const detail = await get(`/entities/${row.id}`)
        const values = {}
        for (const observation of detail.observations || []) {
          if (facetFields.value.includes(observation.semantic_field)) {
            values[observation.semantic_field] = observation.value
          }
        }
        facets.value = Object.assign({}, facets.value, { [row.id]: values })
      } catch (err) {
        facets.value = Object.assign({}, facets.value, { [row.id]: {} })
      }
    }
  }
  await Promise.all(Array.from({ length: ENRICH_CONCURRENCY }, run))
  enriching.value = false
}

function pick(row) {
  selected.value = row
  store.selectEntity(row.id)
  store.setView('explore')
}

const FACET_LABELS = { severity: '严重度', state: '状态' }

/** 表格列：补列插在「类型」之后，而不是排到最后 —— 严重度是漏洞镜头里
 *  第一眼要看的东西，把它挤到「最近」右边等于没有。 */
const columns = computed(() => {
  const out = COLUMNS.slice()
  const extra = facetFields.value.map((field) => ({
    key: field, label: FACET_LABELS[field] || field, width: '78px',
  }))
  out.splice(2, 0, ...extra)
  return out
})

/**
 * 表格行 = 通过筛选的行 + 补回来的那几列；「全部」模式下再按归属**分段**。
 *
 * 分段行是 `{__group, __key}` 的标记行（VirtualTable 跨列渲染、不吃点击）。
 * 顺序：域名按字典序，**「未归属」垫底** —— 它们不是"另一个域名"，是还没归
 * 上；混在中间会把可读的分段冲散。行的分组键就是后端注解的 `domain` 字段，
 * 与 `under_domain=` 筛选同源（单真相），所以两处不会给出不同的归属。
 */
const tableRows = computed(() => {
  const merged = visibleRows.value.map(
    (row) => Object.assign({}, row, facets.value[row.id] || {}))
  if (domainChoice.value) return merged        // 已按域名筛过：一个分段，无须标
  const buckets = new Map()
  for (const row of merged) {
    const name = row.domain || null
    if (!buckets.has(name)) buckets.set(name, [])
    buckets.get(name).push(row)
  }
  const names = [...buckets.keys()].filter((name) => name !== null).sort()
  if (buckets.has(null)) names.push(null)
  const out = []
  for (const name of names) {
    out.push({ __group: name === null ? '未归属' : name,
               __key: `group:${name === null ? 'none' : name}` })
    out.push(...buckets.get(name))
  }
  return out
})

async function onDomainChange(event) {
  domainChoice.value = event.target.value
  selected.value = null
  await load()
}

onMounted(load)
// 切换镜头要重载；项目换了也要 —— 行是按项目隔离的。
watch(() => props.lens, load)
// 切项目**清掉域名选择**：上个项目的域名在新项目多半查无此人，带着它进来 =
// 一页永远空着的表，而"为什么空"不在这个页面的视野里。
watch(() => store.project, () => {
  domainChoice.value = ''
  load()
})
// 首次挂载时 `lenses` 可能还没到（App 的 boot 还在拉 meta → lenses）。kind 列表
// 一到就重载，否则这个页面会停在一个"空镜头"上，而它其实只是比数据早了一拍。
watch(() => kinds.value.length, (now, before) => {
  if (now && now !== before) load()
})
</script>

<template>
  <div class="lens">
    <header class="head">
      <h2 class="section">{{ label || lens }} · 镜头</h2>
      <span v-for="entry in kinds" :key="entry.kind" class="chip">
        <span class="swatch" :style="{ background: kindColor(entry.kind) }" />
        <span class="mono">{{ entry.kind }}</span>
        <b>{{ entry.count }}</b>
      </span>
      <!-- 域名下拉（0.19.0）：选项**懒加载**（第一次聚焦才拉，按项目缓存），
           「全部」= 按域名分段看，选中一个 = 服务端 under_domain= 裁剪。 -->
      <label class="domain-pick">
        <span class="faint">域名</span>
        <select :value="domainChoice" @focus="store.loadDomains()"
                @change="onDomainChange">
          <option value="">全部</option>
          <option v-for="key in domainOptions" :key="key" :value="key">
            {{ key }}
          </option>
        </select>
      </label>
      <span class="spacer" />
      <button class="ghost" :disabled="loading" @click="load">刷新</button>
    </header>

    <p v-if="error" class="bad small">{{ error }}</p>

    <div v-if="facetFields.length" class="facets">
      <label v-for="field in facetFields" :key="field" class="facet">
        <span class="faint">{{ FACET_LABELS[field] || field }}</span>
        <select :value="facetFilter[field]"
                @change="setFacet(field, $event.target.value)">
          <option value="">全部</option>
          <option v-for="value in options(field)" :key="value" :value="value">
            {{ value }}
          </option>
        </select>
      </label>
      <span v-if="enriching" class="faint small">正在读取观测…</span>
      <span v-else-if="visibleRows.length !== rows.length" class="faint small">
        {{ visibleRows.length }} / {{ rows.length }} 条
      </span>
    </div>

    <div class="table-wrap">
      <VirtualTable
        :rows="tableRows"
        :columns="columns"
        :selected-key="selected ? selected.id : null"
        empty-text="载入中…"
        @row-click="pick"
      >
        <template #empty>
          <EmptyState
            v-if="loading"
            title="载入中…"
            desc="正在按类型逐个查询这个镜头。"
          />
          <EmptyState
            v-else
            title="暂无数据"
            :desc="`这个镜头下还没有${label || lens}实体 —— 导入一份 httpx 或 nuclei 输出就会出现。`"
          >
            <button class="primary" @click="emit('import')">立即导入数据</button>
          </EmptyState>
        </template>
        <template #entity_key="{ row }">
          <span class="swatch inline" :style="{ background: kindColor(row.kind) }" />
          {{ row.entity_key }}
        </template>
      </VirtualTable>
    </div>

    <footer class="foot">
      <!-- 数据边界（500 条上限）是**警告**，保持可见；实现细节是**说明**，
           折叠 —— 页脚不再一排密麻小字。 -->
      <span v-if="truncated.length" class="warn small">
        ⚠ {{ truncated.join(' / ') }} 已达 {{ PAGE }} 条上限，本页只显示前 {{ PAGE }} 条。
      </span>
      <details class="tip">
        <summary>这一页怎么查的 / 补齐了什么</summary>
        <div class="body">
          点一行会在画布上打开它的详情。过滤走 SQL（逐个 kind 查询），不是在浏览器里
          筛全库 —— 计划书 §7.5 的「排序过滤在 SQL」。
          <template v-if="facetFields.length">
            严重度 / 状态是这两个实体上的观测：本页只对已载入的这 {{ rows.length }} 条
            补齐它们（走详情面板用的同一个 <span class="mono">GET /entities/{id}</span>），
            服务端筛选是 3.5 的 <span class="mono">core/query.py</span> 的事。
          </template>
        </div>
      </details>
    </footer>
  </div>
</template>

<style scoped>
.lens { display: flex; flex-direction: column; height: 100%; padding: 12px; gap: 8px; }
.head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.facets { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.facet, .domain-pick {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
}
.facet select, .domain-pick select {
  background: var(--panel-2);
  color: var(--fg);
  border: 1px solid var(--line);
  border-radius: var(--radius-ui);
  padding: 2px 6px;
  font-size: 11px;
}
.spacer { flex: 1; }
.table-wrap { flex: 1; min-height: 0; }
.small { font-size: 12px; }
.foot { display: flex; gap: 12px; flex-wrap: wrap; align-items: flex-start; }
.foot .tip { flex: 1; min-width: 260px; }
.swatch { width: 8px; height: 8px; border-radius: 2px; display: inline-block; }
.swatch.inline { margin-right: 6px; vertical-align: middle; }
</style>
