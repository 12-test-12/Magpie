<script setup>
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { store } from '../store.js'
import { fmtInt } from '../format.js'
import { params, remember, requestSlot, workspaceMemory, workspacePages } from '../workspace.js'
import WorkspaceDetail from '../components/WorkspaceDetail.vue'

const props = defineProps({ category: { type: String, required: true } })
const emit = defineEmits(['import'])
const page = computed(() => workspacePages[props.category])
const simple = computed(() => ['evidence', 'notes'].includes(props.category))
const state = reactive({ q: '', source: '', method: '', host: null, prefix: null, mode: 'host', cursor: null })
const draft = reactive({ q: '', source: '', method: '' })
const data = ref(null), error = ref(''), loading = ref(false), selected = ref(null), scroller = ref(null)
const history = ref([]), trail = ref([])
const slot = requestSlot()
let context = '', scroll = 0
const pageIndex = computed(() => history.value.length + 1)

function save() {
  if (context) remember(context, { state: { ...state }, history: history.value, trail: trail.value,
    scroll: scroller.value?.scrollTop || 0 })
}
async function load() {
  selected.value = null
  if (!store.project) { data.value = null; return }
  loading.value = true; error.value = ''; data.value = null
  await slot.run('/workspace/browse?' + params({ project: store.project, category: props.category, ...state, limit: 50 }),
    result => { data.value = result }, message => { error.value = message }, () => { loading.value = false })
  await nextTick()
  if (scroller.value) scroller.value.scrollTop = scroll
}
function resetPage() { state.cursor = null; history.value = []; scroll = 0 }
function filter() {
  Object.assign(state, draft); state.host = null; state.prefix = null
  state.mode = simple.value ? 'list' : 'host'; trail.value = []; resetPage(); load()
}
function descend(row) {
  trail.value.push({ state: { ...state }, history: [...history.value], scroll: scroller.value?.scrollTop || 0 })
  if (state.mode === 'host') {
    state.host = row.label
    state.mode = ['api', 'js'].includes(props.category) ? 'path' : 'list'
  } else { state.prefix = row.label; state.mode = 'list' }
  resetPage(); load()
}
function back() {
  const last = trail.value.pop()
  if (!last) return
  Object.assign(state, last.state); history.value = last.history; scroll = last.scroll; load()
}
function allRows() {
  trail.value.push({ state: { ...state }, history: [...history.value], scroll: scroller.value?.scrollTop || 0 })
  state.mode = 'list'; resetPage(); load()
}
function turn(forward) {
  if (forward) { history.value.push(state.cursor); state.cursor = data.value.next_cursor }
  else state.cursor = history.value.pop()
  scroll = 0; load()
}
function observation(row, names) {
  return (row.observed || []).filter(o => names.includes(o.semantic_field)).map(o => o.value).join(' · ') || '未观察'
}
watch(() => [store.project, props.category], () => {
  save(); slot.cancel(); context = `${store.project}:${props.category}`
  const saved = workspaceMemory.get(context)
  Object.assign(state, saved?.state || { q: '', source: '', method: '', host: null, prefix: null,
    mode: simple.value ? 'list' : 'host', cursor: null })
  Object.assign(draft, { q: state.q, source: state.source, method: state.method })
  history.value = saved?.history || []; trail.value = saved?.trail || []; scroll = saved?.scroll || 0
  load()
}, { immediate: true })
onBeforeUnmount(() => { save(); slot.cancel() })
watch(() => store.dataRevision, () => { resetPage(); load() })
</script>

<template>
  <div class="research-page workspace-page">
    <header class="page-heading"><div><div class="eyebrow">{{ page.english }}</div><h1>{{ page.label }}</h1><p>{{ page.hint }}</p></div>
      <button class="primary" @click="emit('import')">＋ 导入数据</button></header>
    <div v-if="category === 'api'" class="notice">接口沿用当前规范键；方法、状态和参数属于观测。请求/响应全文请在证据中对照，不能据此推断认证漏洞。</div>
    <div v-if="category === 'js'" class="notice">当前仅提供资源地址与来源。未建立 JS 内容关联，内容可用性未知；未执行 AST、SourceMap 或敏感信息分析。</div>
    <form class="workspace-filters" @submit.prevent="filter">
      <label class="search-field"><span>搜索</span><input v-model="draft.q" aria-label="搜索当前工作区" placeholder="输入名称、主机或路径…" maxlength="300" /></label>
      <template v-if="!simple">
        <label>来源<input v-model="draft.source" aria-label="来源名称" placeholder="精确来源名称" maxlength="200" /></label>
        <label v-if="category === 'api' || category === 'js'">方法<select v-model="draft.method" aria-label="HTTP 方法"><option value="">全部方法</option><option v-for="method in ['GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS']" :key="method">{{ method }}</option></select></label>
      </template><button type="submit">应用筛选</button>
      <button type="button" class="ghost" @click="Object.assign(draft, {q:'',source:'',method:''}); filter()">重置</button>
    </form>
    <div class="workspace-location">
      <div class="breadcrumbs"><button v-if="trail.length" class="ghost" @click="back">← 返回上级</button><span>{{ page.label }}</span><span v-if="state.host !== null">/ {{ state.host || '未关联主机' }}</span><span v-if="state.prefix !== null">/ {{ state.prefix }}</span></div>
      <button v-if="!simple && state.mode !== 'list'" class="ghost" @click="allRows">查看当前范围的全部结果 →</button>
      <button class="ghost" :disabled="loading" @click="load">刷新</button>
    </div>
    <div v-if="!store.project" class="workspace-state">先创建或选择项目，再导入研究数据。</div>
    <div v-else-if="loading" class="workspace-state" role="status">正在查询工作区…</div>
    <div v-else-if="error" class="workspace-state error-state" role="alert"><strong>无法载入结果</strong><p>{{ error }}</p><button @click="load">重试</button><button @click="filter">从第一页重查</button></div>
    <template v-else-if="data">
      <div class="result-summary"><strong>{{ fmtInt(data.total) }}</strong> {{ state.mode === 'list' ? '条结果' : '个分组' }}<span v-if="data.entity_total !== undefined"> · {{ fmtInt(data.entity_total) }} 个对象</span><span class="dim">按{{ state.mode === 'host' ? '精确主机' : state.mode === 'path' ? '规范路径前两段' : '稳定 ID' }}排序 · 每页最多 {{ data.limit }} 条</span></div>
      <div class="workspace-body">
        <div ref="scroller" class="workspace-results">
          <div v-if="!data.rows.length" class="workspace-state"><strong>没有符合条件的结果</strong><p>调整筛选或导入对应数据。未识别内容可在导入覆盖报告中检查。</p><button @click="emit('import')">导入数据</button></div>
          <div v-else-if="state.mode !== 'list'" class="group-grid">
            <button v-for="row in data.rows" :key="row.group_key" class="group-card" @click="descend(row)"><span class="group-icon">{{ state.mode === 'host' ? '◎' : '↳' }}</span><strong :title="row.label">{{ row.label || '未关联主机' }}</strong><span class="dim">{{ fmtInt(row.count) }} 个对象</span><span class="group-action">下钻查看 ↗</span></button>
          </div>
          <table v-else class="workspace-table"><thead><tr><th>{{ simple ? '来源 / 对象' : '研究对象' }}</th><th>类型</th><th>{{ category === 'evidence' ? '原文 / 恢复状态' : category === 'notes' ? '笔记内容' : '观测 / 状态' }}</th></tr></thead><tbody>
            <tr v-for="row in data.rows" :key="row.id" :class="{ selected: selected?.id === row.id }"><td><button class="row-link mono" :title="row.entity_key" @click="selected = row">{{ row.entity_key }}</button><small v-if="row.host">{{ row.host }}</small></td><td><span class="type-label">{{ row.kind }}</span></td><td><template v-if="category === 'evidence'">{{ row.storage_state }} / {{ row.status }}</template><template v-else-if="category === 'notes'"><span class="note-excerpt">{{ row.text }}</span></template><template v-else-if="category === 'js'">地址已导入 · 内容关联未知</template><template v-else-if="category === 'api'"><span class="method-label">{{ observation(row, ['method','http.method']) }}</span> {{ observation(row, ['status','status_code','http.status','http.status_code']) }}<small v-if="row.observed_truncated" class="dim">摘要已截断，更多观测见详情</small></template><template v-else>{{ row.lifecycle === 'active' ? '活跃' : '退役' }}</template></td></tr>
          </tbody></table>
        </div>
        <WorkspaceDetail v-if="selected" :key="`${category}:${selected.id}`" :row="selected" :category="category" @close="selected = null" @select="selected = {id: $event, kind: 'entity'}" />
      </div>
      <footer class="workspace-pagination"><span class="dim">当前显示 {{ data.rows.length }} 条 · 第 {{ pageIndex }} 页</span><button :disabled="!history.length" @click="turn(false)">上一页</button><button :disabled="!data.next_cursor" @click="turn(true)">下一页</button></footer>
    </template>
  </div>
</template>
