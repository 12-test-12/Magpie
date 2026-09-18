<script setup>
/**
 * 镜头页（计划书 §7.4）。本阶段先做**资产**与**服务与中间件**两个。
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
 */
import { computed, onMounted, ref, watch } from 'vue'
import VirtualTable from '../components/VirtualTable.vue'
import { get } from '../api.js'
import { store } from '../store.js'
import { kindColor } from '../graph.js'

const props = defineProps({
  lens: { type: String, required: true },
  label: { type: String, default: '' },
})

const PAGE = 500

const rows = ref([])
const loading = ref(false)
const error = ref('')
const selected = ref(null)
const truncated = ref([])

const COLUMNS = [
  { key: 'entity_key', label: '实体', mono: true },
  { key: 'kind', label: '类型', width: '86px' },
  { key: 'observation_count', label: '观测', mono: true, width: '60px' },
  { key: 'out_degree', label: '出边', mono: true, width: '60px' },
  { key: 'first_seen', label: '首次', mono: true, width: '170px' },
  { key: 'last_seen', label: '最近', mono: true, width: '170px' },
]

const kinds = computed(() => (store.lenses[props.lens]?.kinds || []))

async function load() {
  loading.value = true
  error.value = ''
  selected.value = null
  const collected = []
  const capped = []
  try {
    for (const entry of kinds.value) {
      const query = store.project ? `&project=${encodeURIComponent(store.project)}` : ''
      const data = await get(
        `/entities?kind=${encodeURIComponent(entry.kind)}&limit=${PAGE}${query}`)
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
}

function pick(row) {
  selected.value = row
  store.selectEntity(row.id)
  store.setView('explore')
}

onMounted(load)
// 切换镜头要重载；项目换了也要 —— 行是按项目隔离的。
watch(() => props.lens, load)
watch(() => store.project, load)
// 首次挂载时 `lenses` 可能还没到（App 的 boot 在并行拉）。kind 列表一到就重载，
// 否则这个页面会停在一个"空镜头"上，而它其实只是比数据早了一拍。
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
      <span class="spacer" />
      <button class="ghost" :disabled="loading" @click="load">刷新</button>
    </header>

    <p v-if="error" class="bad small">{{ error }}</p>

    <div class="table-wrap">
      <VirtualTable
        :rows="rows"
        :columns="COLUMNS"
        :selected-key="selected ? selected.id : null"
        :empty-text="loading ? '载入中…' : '这个镜头下还没有实体 —— 先导入一份 httpx 输出试试。'"
        @row-click="pick"
      >
        <template #entity_key="{ row }">
          <span class="swatch inline" :style="{ background: kindColor(row.kind) }" />
          {{ row.entity_key }}
        </template>
      </VirtualTable>
    </div>

    <footer class="foot faint">
      <span v-if="truncated.length" class="warn">
        ⚠ {{ truncated.join(' / ') }} 已达 {{ PAGE }} 条上限，本页只显示前 {{ PAGE }} 条。
      </span>
      <span>
        点一行会在画布上打开它的详情。过滤走 SQL（逐个 kind 查询），
        不是在浏览器里筛全库 —— 计划书 §7.5 的"排序过滤在 SQL"。
      </span>
    </footer>
  </div>
</template>

<style scoped>
.lens { display: flex; flex-direction: column; height: 100%; padding: 12px; gap: 8px; }
.head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.spacer { flex: 1; }
.table-wrap { flex: 1; min-height: 0; }
.small { font-size: 11px; }
.foot { font-size: 11px; display: flex; gap: 12px; flex-wrap: wrap; }
.swatch { width: 8px; height: 8px; border-radius: 2px; display: inline-block; }
.swatch.inline { margin-right: 6px; vertical-align: middle; }
</style>
