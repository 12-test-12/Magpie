<script setup>
/**
 * Inbox 页（计划书 §7.4）。
 *
 * 「清空即干净」—— 这是这一页唯一要传达的状态。所以顶部永远显示**还剩几条**，
 * 而不是"共几页"：一个待办队列的页数没有意义，剩余数才有。
 *
 * 裁决走 `POST /inbox/{id}/resolve`（2.7 就有的端点，写者唯一）。默认只看
 * `state=open` —— 已裁决的项属于历史，不占这个页面的注意力。
 */
import { computed, onMounted, ref, watch } from 'vue'
import VirtualTable from '../components/VirtualTable.vue'
import { get, post } from '../api.js'
import { store } from '../store.js'

const SEVERITY_ORDER = { high: 0, medium: 1, low: 2 }
const SEVERITY_LABEL = { high: '高', medium: '中', low: '低' }

//: kind → 中文。**键必须与 schema 的 `inbox_items.kind` CHECK 逐个对得上**
//: （`001_init.sql` 的七个）：2.8 的初版名单（`sniff_uncertain` /
//: `unregistered_host` / `duplicate_entity` / `out_of_scope` / `version_conflict`）
//: 一个都不是库里真有的 kind —— 那些名字来自计划书早期的草稿，而数据层从 001 起
//: 就冻结了另一套。结果是**每一条待办都显示成它的英文 kind**，包括那些已经
//: 有中文名字的。3.6 把这张表对齐到七个真 kind。
//:
//: 分类表（哪一类属于体检页的哪一栏）在 `core/query.py` 的 `HEALTH_CATEGORIES`，
//: 不在这里 —— 这里只管翻译。
const KIND_LABEL = {
  orphan_subject: '孤儿实体',
  value_conflict: '字段冲突',
  merge_conflict: '合并冲突',
  identity_ambiguous: '身份不唯一',
  evidence_missing: '未知字段',
  scope_drift: '越界资产',
  manual_required: '嗅探不确定',
}

const rows = ref([])
const loading = ref(false)
const error = ref('')
const busyId = ref(null)

const COLUMNS = [
  { key: 'severityLabel', label: '严重度', width: '64px' },
  { key: 'kindLabel', label: '类型', width: '120px' },
  { key: 'subject', label: '对象', mono: true },
  { key: 'occurrence_count', label: '次数', mono: true, width: '56px' },
  { key: 'created_at', label: '产生于', mono: true, width: '170px' },
  { key: 'actions', label: '', width: '132px' },
]

const empty = computed(() => !loading.value && !rows.value.length)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const query = store.project ? `&project=${encodeURIComponent(store.project)}` : ''
    const data = await get(`/inbox?state=open${query}`)
    rows.value = data
      .sort((a, b) => (SEVERITY_ORDER[a.severity] ?? 9) - (SEVERITY_ORDER[b.severity] ?? 9))
      .map((row) => Object.assign({}, row, {
        __key: row.id,
        severityLabel: SEVERITY_LABEL[row.severity] || row.severity,
        kindLabel: KIND_LABEL[row.kind] || row.kind,
        subject: `${row.subject_kind} #${row.subject_id}`,
      }))
  } catch (err) {
    error.value = err.message
    rows.value = []
  } finally {
    loading.value = false
  }
}

async function resolve(row, state) {
  busyId.value = row.id
  try {
    await post(`/inbox/${row.id}/resolve`, { state })
    store.pushLog(`Inbox #${row.id} → ${state}`, 'import')
    await load()
    await store.loadMeta()                  // 顶部徽标要跟着掉
  } catch (err) {
    error.value = err.message
  } finally {
    busyId.value = null
  }
}

onMounted(load)
watch(() => store.project, load)
watch(() => store.view, (value) => {
  if (value === 'inbox') load()
})
</script>

<template>
  <div class="inbox">
    <header class="head">
      <h2 class="section">Inbox</h2>
      <span v-if="rows.length" class="chip warn">还剩 {{ rows.length }} 条</span>
      <span v-else-if="!loading" class="chip ok">干净 —— 没有待裁决的项</span>
      <span class="spacer" />
      <button class="ghost" :disabled="loading" @click="load">刷新</button>
    </header>

    <p v-if="error" class="bad small">{{ error }}</p>

    <div class="table-wrap">
      <VirtualTable
        :rows="rows"
        :columns="COLUMNS"
        empty-text="没有待裁决的项。数据层在嗅探拿不准、字段多值、合并后多值、出现未知字段、资产越界时才会往这里放东西。"
        @row-click="() => {}"
      >
        <template #subject="{ row }">
          {{ row.subject }}
        </template>
        <template #actions="{ row }">
          <button :disabled="busyId === row.id" @click.stop="resolve(row, 'resolved')">
            裁决
          </button>
          <button class="ghost" :disabled="busyId === row.id"
                  @click.stop="resolve(row, 'dismissed')">忽略</button>
        </template>
      </VirtualTable>
    </div>

    <footer class="foot faint">
      「裁决」= `POST /inbox/{id}/resolve {state:"resolved"}`。被裁决的项不再出现在
      这个列表里（默认只看 open）。批量裁决与"裁决时顺带改数据"属于后续步骤 ——
      <p v-if="empty" class="dim" style="margin:6px 0 0">
        空 Inbox 不是"没有数据"，而是"数据层没有需要人拿主意的地方"。
      </p>
    </footer>
  </div>
</template>

<style scoped>
.inbox { display: flex; flex-direction: column; height: 100%; padding: 12px; gap: 8px; }
.head { display: flex; align-items: center; gap: 8px; }
.spacer { flex: 1; }
.table-wrap { flex: 1; min-height: 0; }
.small { font-size: 11px; }
.foot { font-size: 11px; line-height: 1.6; }
</style>
