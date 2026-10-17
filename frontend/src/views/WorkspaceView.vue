<script setup>
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { store } from '../store.js'
import { fmtInt } from '../format.js'
import { params, remember, requestSlot, workspaceMemory, workspacePages } from '../workspace.js'
import WorkspaceDetail from '../components/WorkspaceDetail.vue'
import FindingActions from '../components/FindingActions.vue'

const props = defineProps({ category: { type: String, required: true } })
const emit = defineEmits(['import'])
const page = computed(() => workspacePages[props.category])
const simple = computed(() => ['evidence', 'notes'].includes(props.category))
const state = reactive({ q: '', source: '', method: '', status: '', severity: '', tag: '', criticality: null, host: null, prefix: null, mode: 'host', cursor: null })
const draft = reactive({ q: '', source: '', method: '', status: '', severity: '', tag: '', criticality: null })
const data = ref(null), error = ref(''), loading = ref(false), selected = ref(null), scroller = ref(null)
const history = ref([]), trail = ref([])
const selectedCategory = ref(props.category)
const slot = requestSlot()
let context = '', scroll = 0
const pageIndex = computed(() => history.value.length + 1)

function statusLabel(status) {
  return ({
    candidate: '待核实',
    confirmed: '已确认',
    false_positive: '误报',
    duplicate: '重复',
    inconclusive: '存疑',
    resolved: '已修复',
  })[status] || status || '待核实'
}
function severityLabel(sev) {
  return ({
    critical: '严重',
    high: '高危',
    medium: '中危',
    low: '低危',
    info: '提示',
  })[sev] || sev || '未知'
}

function save() {
  if (context) remember(context, { state: { ...state }, history: history.value, trail: trail.value,
    scroll: scroller.value?.scrollTop || 0 })
}
async function load(preserveSelection = false) {
  preserveSelection = preserveSelection === true
  if (!preserveSelection) { selectedCategory.value = props.category; selected.value = null }
  if (!store.project) { data.value = null; return }
  loading.value = true; error.value = ''; if (!preserveSelection) data.value = null
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
function apiMethod(row) {
  if (row.identity_status === 'exact_v2') return row.endpoint_identity?.method || 'unknown'
  return observation(row, ['method', 'http.method'])
}
function selectRelated(value) {
  if (typeof value === 'object') {
    selectedCategory.value = value.category || props.category
    selected.value = { id: value.id, kind: value.kind || 'entity' }
  } else {
    selectedCategory.value = props.category
    selected.value = { id: value, kind: 'entity' }
  }
}
function selectRow(row) {
  selectedCategory.value = props.category
  selected.value = row
}
function jsVersion(row) {
  if (row.js_content) return row.js_content.status === 'content_not_imported' ? null : row.js_content
  const raw = row.observed?.find(o => o.semantic_field === 'js_content_version')?.value
  try { return JSON.parse(raw) } catch { return null }
}
function jsStatus(value) {
  return ({ bounded_rules_complete: '有限规则已完成', partial: '部分内容未理解', limit_exceeded: '超过内容上限',
    base64_invalid: 'HAR base64 解码失败', content_not_imported: '内容未导入' })[value] || value || '状态未知'
}
function hasJsSource(row) {
  return (row.observed || []).some(o => o.semantic_field === 'js_source_ref' || o.semantic_field === 'js_static_method')
}
watch(() => [store.project, props.category], () => {
  selectedCategory.value = props.category
  save(); slot.cancel(); context = `${store.project}:${props.category}`
  const saved = workspaceMemory.get(context)
  Object.assign(state, { tag: '', criticality: null }, saved?.state || { q: '', source: '', method: '', status: '', severity: '', host: null, prefix: null,
    mode: simple.value ? 'list' : 'host', cursor: null })
  Object.assign(draft, { q: state.q, source: state.source, method: state.method, status: state.status, severity: state.severity, tag: state.tag, criticality: state.criticality })
  history.value = saved?.history || []; trail.value = saved?.trail || []; scroll = saved?.scroll || 0
  load()
}, { immediate: true })
watch(() => [store.workspaceJump, props.category], async ([jump]) => {
  if (!jump || jump.project !== store.project || jump.category !== props.category) return
  // Finish restoring the page context before applying the explicit destination.
  // Vue can reuse this component when a search changes the business category.
  await nextTick()
  if (store.workspaceJump !== jump || jump.project !== store.project || jump.category !== props.category) return
  Object.assign(state, {q:'', source:'', method:'', status:'', severity:'', tag:'', criticality:null,
    host:null, prefix:null, mode:'list', cursor:null}, jump.filters)
  Object.assign(draft, {q:state.q, source:state.source, method:state.method, status:state.status, severity:state.severity, tag:state.tag, criticality:state.criticality})
  selectedCategory.value = props.category; selected.value = jump.row
  trail.value = []; resetPage(); store.workspaceJump = null; load(true)
}, { immediate:true })
onBeforeUnmount(() => { save(); slot.cancel() })
watch(() => store.dataRevision, () => { resetPage(); load(true) })
</script>

<template>
  <div class="research-page workspace-page">
    <header class="page-heading">
      <div><div class="eyebrow">{{ page.english }}</div><h1>{{ page.label }}</h1><p>{{ page.hint }}</p></div>
      <div style="display: flex; gap: 10px; align-items: center;">
        <FindingActions v-if="category === 'findings'" @created="resetPage(); load()" />
        <button class="primary" @click="emit('import')">＋ 导入数据</button>
      </div>
    </header>
    <div v-if="category === 'api'" class="notice">接口按 scheme、主机、有效端口、精确路径和方法区分；历史折叠身份会明确标记。请求样本只展示脱敏摘要与参数名，缺少凭据不代表未授权或存在漏洞。</div>
    <div v-if="category === 'js'" class="notice">仅处理已导入的内容（支持包含 JS 响应体的 HAR 或通过 *.manifest.json 导入的独立 JS），按字面量识别静态调用候选；不会执行 JavaScript，也不推断相对 URL 的运行基址。候选引用不代表浏览器实际发送了请求。</div>
    <div v-if="category === 'findings'" class="notice">工具扫描命中默认归为待核实（Candidate）；严禁将未经核实的外部扫描结果直接视作已确认漏洞。复测记录须保留证据定位，网络超时或不可达严禁判定为已修复。</div>
    <form class="workspace-filters" @submit.prevent="filter">
      <label class="search-field"><span>搜索</span><input v-model="draft.q" aria-label="搜索当前工作区" placeholder="输入名称、主机或路径…" maxlength="300" /></label>
      <template v-if="!simple">
        <template v-if="['assets','api','js'].includes(category)"><label>人工标签<input v-model="draft.tag" aria-label="按人工标签筛选" placeholder="精确标签" maxlength="60" /></label><label>重要度<select v-model="draft.criticality" aria-label="按重要度筛选"><option :value="null">全部重要度</option><option v-for="tier in [1,2,3]" :key="tier" :value="tier">Tier {{ tier }}</option></select></label></template>
        <label v-if="category !== 'findings'">来源<input v-model="draft.source" aria-label="来源名称" placeholder="精确来源名称" maxlength="200" /></label>
        <label v-if="category === 'api' || category === 'js'">方法<select v-model="draft.method" aria-label="HTTP 方法"><option value="">全部方法</option><option value="unknown">方法未知</option><option v-for="method in ['GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS']" :key="method">{{ method }}</option></select></label>
        <label v-if="category === 'findings'">处置状态<select v-model="draft.status" aria-label="处置状态"><option value="">全部状态</option><option value="candidate">待核实 (candidate)</option><option value="confirmed">已确认 (confirmed)</option><option value="resolved">已修复 (resolved)</option><option value="false_positive">误报排除 (false_positive)</option><option value="duplicate">重复发现 (duplicate)</option><option value="inconclusive">存疑 (inconclusive)</option></select></label>
        <label v-if="category === 'findings'">严重级别<select v-model="draft.severity" aria-label="严重级别"><option value="">全部级别</option><option value="critical">严重 (critical)</option><option value="high">高危 (high)</option><option value="medium">中危 (medium)</option><option value="low">低危 (low)</option><option value="info">提示 (info)</option></select></label>
      </template>
      <button type="submit">应用筛选</button>
      <button type="button" class="ghost" @click="Object.assign(draft, {q:'',source:'',method:'',status:'',severity:'',tag:'',criticality:null}); filter()">重置</button>
    </form>
    <div class="workspace-location">
      <div class="breadcrumbs"><button v-if="trail.length" class="ghost" @click="back">← 返回上级</button><span>{{ page.label }}</span><span v-if="state.host !== null">/ {{ state.host || '未关联主机' }}</span><span v-if="state.prefix !== null">/ {{ state.prefix }}</span></div>
      <button v-if="!simple && state.mode !== 'list'" class="ghost" @click="allRows">查看当前范围的全部结果 →</button>
      <button class="ghost" :disabled="loading" @click="load">刷新</button>
    </div>
    <div v-if="!store.project" class="workspace-state">先创建或选择项目，再导入研究数据。</div>
    <div v-else-if="loading && !data" class="workspace-state" role="status">正在查询工作区…</div>
    <div v-else-if="error" class="workspace-state error-state" role="alert"><strong>无法载入结果</strong><p>{{ error }}</p><button @click="load">重试</button><button @click="filter">从第一页重查</button></div>
    <template v-else-if="data">
      <div class="result-summary"><strong>{{ fmtInt(data.total) }}</strong> {{ state.mode === 'list' ? '条结果' : '个分组' }}<span v-if="data.entity_total !== undefined"> · {{ fmtInt(data.entity_total) }} 个对象</span><span class="dim">按{{ state.mode === 'host' ? '精确主机' : state.mode === 'path' ? '精确路径前缀（前两段，仅用于分组）' : '稳定 ID' }}排序 · 每页最多 {{ data.limit }} 条</span></div>
      <div class="workspace-body">
        <div ref="scroller" class="workspace-results">
          <div v-if="!data.rows.length" class="workspace-state"><strong>没有符合条件的结果</strong><p>调整筛选或导入对应数据。未识别内容可在导入覆盖报告中检查。</p><button @click="emit('import')">导入数据</button></div>
          <div v-else-if="state.mode !== 'list'" class="group-grid">
            <button v-for="row in data.rows" :key="row.group_key" class="group-card" @click="descend(row)"><span class="group-icon">{{ state.mode === 'host' ? '◎' : '↳' }}</span><strong :title="row.label">{{ row.label || '未关联主机' }}</strong><span class="dim">{{ fmtInt(row.count) }} 个对象</span><span class="group-action">下钻查看 ↗</span></button>
          </div>
          <table v-else class="workspace-table"><thead><tr><th>{{ simple ? '来源 / 对象' : category === 'findings' ? '漏洞名称 / 标识' : '研究对象' }}</th><th>类型</th><th>{{ category === 'evidence' ? '原文 / 恢复状态' : category === 'notes' ? '笔记内容' : category === 'findings' ? '严重度 · 处置状态 · 影响目标' : '观测 / 状态' }}</th></tr></thead><tbody>
            <tr v-for="row in data.rows" :key="row.id" :class="{ selected: selected?.id === row.id }"><td><button class="row-link mono" :title="row.entity_key" @click="selectRow(row)">{{ row.title || row.display_key || row.entity_key }}</button><div v-if="row.criticality || row.tags?.length" class="research-badges"><span v-if="row.criticality" class="tier-badge" :class="{priority:row.criticality === 1}">★ Tier {{ row.criticality }}</span><span v-for="tag in (row.tags || []).slice(0,8)" :key="tag" class="type-label">{{ tag }}</span><small v-if="row.tags?.length > 8">更多标签见详情</small></div><small v-if="row.host">{{ row.host }}</small><small v-if="row.identity_status === 'legacy_folded'" class="warn">历史路径模板身份</small></td><td><span class="type-label">{{ row.kind }}</span></td><td><template v-if="category === 'evidence'">{{ row.storage_state }} / {{ row.status }}</template><template v-else-if="category === 'notes'"><span class="note-excerpt">{{ row.text }}</span><small v-if="row.target_display" class="dim" style="display: block; margin-top: 4px;">关联目标: <button class="ghost inline-link" @click.stop="selectRelated({ id: row.entity_id, kind: row.target_kind || 'entity' })">{{ row.target_display }}</button></small><small v-if="row.evidence_id" class="dim" style="display: block; margin-top: 2px;">来源证据: <button class="ghost inline-link" @click.stop="selectRelated({ id: row.evidence_id, kind: 'evidence' })">#{{ row.evidence_id }}</button></small></template><template v-else-if="category === 'findings'"><div style="display: flex; align-items: center; gap: 6px; flex-wrap: wrap;"><span :class="['badge-severity', 'badge-' + (row.severity || 'info')]">{{ (row.severity || 'unknown').toUpperCase() }}</span><span :class="['badge-status', 'status-' + (row.status || 'candidate')]">{{ statusLabel(row.status) }}</span><span v-if="row.target_asset" class="dim" style="font-size: 11px;">目标: {{ row.target_asset.display || row.target_asset.display_key }}</span><small v-if="row.retests_count" class="dim">（{{ row.retests_count }} 次复测）</small></div><small v-if="row.disposition_reason" class="dim" style="display: block; margin-top: 4px;">{{ row.disposition_reason }}</small></template><template v-else-if="category === 'js'"><span>{{ jsStatus(jsVersion(row)?.status || 'content_not_imported') }}</span><small v-if="jsVersion(row)?.candidate_count !== undefined">{{ jsVersion(row).candidate_count }} 个候选</small><small v-if="jsVersion(row)?.sha256" class="mono" :title="jsVersion(row).sha256">SHA-256 {{ jsVersion(row).sha256.slice(0, 12) }}…</small><small v-if="jsVersion(row)?.reason" class="warn">{{ jsVersion(row).reason }}</small></template><template v-else-if="category === 'api'"><span class="method-label">{{ apiMethod(row) }}</span> {{ observation(row, ['status','status_code','http.status','http.status_code']) }}<small v-if="observation(row, ['declared_method']) !== '未观察'">声明方法 {{ observation(row, ['declared_method']) }}</small><small v-if="hasJsSource(row)" class="type-label" title="该接口包含来自 JavaScript 源码的静态引用候选">JS 候选</small><small v-if="row.observed_truncated" class="dim">摘要已截断，更多观测见详情</small></template><template v-else>{{ row.lifecycle === 'active' ? '活跃' : '退役' }}</template></td></tr>
          </tbody></table>
        </div>
        <WorkspaceDetail v-if="selected" :key="`${selectedCategory}:${selected.id}`" :row="selected" :category="selectedCategory" @close="selected = null" @select="selectRelated" />
      </div>
      <footer class="workspace-pagination"><span class="dim">当前显示 {{ data.rows.length }} 条 · 第 {{ pageIndex }} 页</span><button :disabled="!history.length" @click="turn(false)">上一页</button><button :disabled="!data.next_cursor" @click="turn(true)">下一页</button></footer>
    </template>

  </div>
</template>
