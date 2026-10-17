<script setup>
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { store } from '../store.js'
import { fmtInt } from '../format.js'
import { API_PREFIX, get, post, token } from '../api.js'
import { params, remember, requestSlot, workspaceMemory, workspacePages } from '../workspace.js'
import WorkspaceDetail from '../components/WorkspaceDetail.vue'

const props = defineProps({ category: { type: String, required: true } })
const emit = defineEmits(['import'])
const page = computed(() => workspacePages[props.category])
const simple = computed(() => ['evidence', 'notes'].includes(props.category))
const state = reactive({ q: '', source: '', method: '', status: '', severity: '', host: null, prefix: null, mode: 'host', cursor: null })
const draft = reactive({ q: '', source: '', method: '', status: '', severity: '' })
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

const showCreateModal = ref(false)
const createForm = reactive({ title: '', target_id: '', severity: 'high', status: 'confirmed', reason: '', description: '' })
const createSubmitting = ref(false)
const createError = ref('')

async function submitCreateFinding() {
  createError.value = ''
  if (!createForm.title.trim()) { createError.value = '漏洞标题不能为空'; return }
  if (!createForm.target_id) { createError.value = '目标实体 ID 不能为空'; return }
  createSubmitting.value = true
  try {
    await post('/workspace/findings?' + params({ project: store.project }), {
      title: createForm.title.trim(),
      target_id: Number(createForm.target_id),
      severity: createForm.severity,
      status: createForm.status,
      reason: createForm.reason.trim() || undefined,
      description: createForm.description.trim() || undefined,
    })
    showCreateModal.value = false
    Object.assign(createForm, { title: '', target_id: '', severity: 'high', status: 'confirmed', reason: '', description: '' })
    resetPage()
    load()
  } catch (err) {
    createError.value = err.message
  } finally {
    createSubmitting.value = false
  }
}

const showExportModal = ref(false)
const exporting = ref(false)
const exportError = ref('')

async function downloadReport(format) {
  exportError.value = ''
  exporting.value = true
  try {
    if (format === 'json') {
      const reportData = await get('/workspace/report?' + params({ project: store.project, format: 'json' }))
      const blob = new Blob([JSON.stringify(reportData, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `magpie-report-${store.project}.json`
      a.click()
      URL.revokeObjectURL(url)
    } else {
      const res = await fetch(`${API_PREFIX}/workspace/report?` + params({ project: store.project, format: 'markdown' }), {
        headers: { Authorization: `Bearer ${token.value}` },
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const text = await res.text()
      const blob = new Blob([text], { type: 'text/markdown; charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `magpie-report-${store.project}.md`
      a.click()
      URL.revokeObjectURL(url)
    }
  } catch (err) {
    exportError.value = err.message
  } finally {
    exporting.value = false
  }
}

function save() {
  if (context) remember(context, { state: { ...state }, history: history.value, trail: trail.value,
    scroll: scroller.value?.scrollTop || 0 })
}
async function load() {
  selectedCategory.value = props.category
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
  Object.assign(state, saved?.state || { q: '', source: '', method: '', status: '', severity: '', host: null, prefix: null,
    mode: simple.value ? 'list' : 'host', cursor: null })
  Object.assign(draft, { q: state.q, source: state.source, method: state.method, status: state.status, severity: state.severity })
  history.value = saved?.history || []; trail.value = saved?.trail || []; scroll = saved?.scroll || 0
  load()
}, { immediate: true })
onBeforeUnmount(() => { save(); slot.cancel() })
watch(() => store.dataRevision, () => { resetPage(); load() })
</script>

<template>
  <div class="research-page workspace-page">
    <header class="page-heading">
      <div><div class="eyebrow">{{ page.english }}</div><h1>{{ page.label }}</h1><p>{{ page.hint }}</p></div>
      <div style="display: flex; gap: 10px; align-items: center;">
        <button v-if="category === 'findings'" class="ghost" @click="showCreateModal = true">＋ 人工建发现</button>
        <button v-if="category === 'findings'" class="ghost" @click="showExportModal = true">📄 导出报告</button>
        <button class="primary" @click="emit('import')">＋ 导入数据</button>
      </div>
    </header>
    <div v-if="category === 'api'" class="notice">接口按 scheme、主机、有效端口、精确路径和方法区分；历史折叠身份会明确标记。请求样本只展示脱敏摘要与参数名，缺少凭据不代表未授权或存在漏洞。</div>
    <div v-if="category === 'js'" class="notice">仅处理已导入的内容（支持包含 JS 响应体的 HAR 或通过 *.manifest.json 导入的独立 JS），按字面量识别静态调用候选；不会执行 JavaScript，也不推断相对 URL 的运行基址。候选引用不代表浏览器实际发送了请求。</div>
    <div v-if="category === 'findings'" class="notice">工具扫描命中默认归为待核实（Candidate）；严禁将未经核实的外部扫描结果直接视作已确认漏洞。复测记录须保留证据定位，网络超时或不可达严禁判定为已修复。</div>
    <form class="workspace-filters" @submit.prevent="filter">
      <label class="search-field"><span>搜索</span><input v-model="draft.q" aria-label="搜索当前工作区" placeholder="输入名称、主机或路径…" maxlength="300" /></label>
      <template v-if="!simple">
        <label v-if="category !== 'findings'">来源<input v-model="draft.source" aria-label="来源名称" placeholder="精确来源名称" maxlength="200" /></label>
        <label v-if="category === 'api' || category === 'js'">方法<select v-model="draft.method" aria-label="HTTP 方法"><option value="">全部方法</option><option value="unknown">方法未知</option><option v-for="method in ['GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS']" :key="method">{{ method }}</option></select></label>
        <label v-if="category === 'findings'">处置状态<select v-model="draft.status" aria-label="处置状态"><option value="">全部状态</option><option value="candidate">待核实 (candidate)</option><option value="confirmed">已确认 (confirmed)</option><option value="resolved">已修复 (resolved)</option><option value="false_positive">误报排除 (false_positive)</option><option value="duplicate">重复发现 (duplicate)</option><option value="inconclusive">存疑 (inconclusive)</option></select></label>
        <label v-if="category === 'findings'">严重级别<select v-model="draft.severity" aria-label="严重级别"><option value="">全部级别</option><option value="critical">严重 (critical)</option><option value="high">高危 (high)</option><option value="medium">中危 (medium)</option><option value="low">低危 (low)</option><option value="info">提示 (info)</option></select></label>
      </template>
      <button type="submit">应用筛选</button>
      <button type="button" class="ghost" @click="Object.assign(draft, {q:'',source:'',method:'',status:'',severity:''}); filter()">重置</button>
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
      <div class="result-summary"><strong>{{ fmtInt(data.total) }}</strong> {{ state.mode === 'list' ? '条结果' : '个分组' }}<span v-if="data.entity_total !== undefined"> · {{ fmtInt(data.entity_total) }} 个对象</span><span class="dim">按{{ state.mode === 'host' ? '精确主机' : state.mode === 'path' ? '精确路径前缀（前两段，仅用于分组）' : '稳定 ID' }}排序 · 每页最多 {{ data.limit }} 条</span></div>
      <div class="workspace-body">
        <div ref="scroller" class="workspace-results">
          <div v-if="!data.rows.length" class="workspace-state"><strong>没有符合条件的结果</strong><p>调整筛选或导入对应数据。未识别内容可在导入覆盖报告中检查。</p><button @click="emit('import')">导入数据</button></div>
          <div v-else-if="state.mode !== 'list'" class="group-grid">
            <button v-for="row in data.rows" :key="row.group_key" class="group-card" @click="descend(row)"><span class="group-icon">{{ state.mode === 'host' ? '◎' : '↳' }}</span><strong :title="row.label">{{ row.label || '未关联主机' }}</strong><span class="dim">{{ fmtInt(row.count) }} 个对象</span><span class="group-action">下钻查看 ↗</span></button>
          </div>
          <table v-else class="workspace-table"><thead><tr><th>{{ simple ? '来源 / 对象' : category === 'findings' ? '漏洞名称 / 标识' : '研究对象' }}</th><th>类型</th><th>{{ category === 'evidence' ? '原文 / 恢复状态' : category === 'notes' ? '笔记内容' : category === 'findings' ? '严重度 · 处置状态 · 影响目标' : '观测 / 状态' }}</th></tr></thead><tbody>
            <tr v-for="row in data.rows" :key="row.id" :class="{ selected: selected?.id === row.id }"><td><button class="row-link mono" :title="row.entity_key" @click="selectRow(row)">{{ row.title || row.display_key || row.entity_key }}</button><small v-if="row.host">{{ row.host }}</small><small v-if="row.identity_status === 'legacy_folded'" class="warn">历史路径模板身份</small></td><td><span class="type-label">{{ row.kind }}</span></td><td><template v-if="category === 'evidence'">{{ row.storage_state }} / {{ row.status }}</template><template v-else-if="category === 'notes'"><span class="note-excerpt">{{ row.text }}</span><small v-if="row.target_display" class="dim" style="display: block; margin-top: 4px;">关联目标: <button class="ghost inline-link" @click.stop="selectRelated({ id: row.entity_id, kind: row.target_kind || 'entity' })">{{ row.target_display }}</button></small><small v-if="row.evidence_id" class="dim" style="display: block; margin-top: 2px;">来源证据: <button class="ghost inline-link" @click.stop="selectRelated({ id: row.evidence_id, kind: 'evidence' })">#{{ row.evidence_id }}</button></small></template><template v-else-if="category === 'findings'"><div style="display: flex; align-items: center; gap: 6px; flex-wrap: wrap;"><span :class="['badge-severity', 'badge-' + (row.severity || 'info')]">{{ (row.severity || 'unknown').toUpperCase() }}</span><span :class="['badge-status', 'status-' + (row.status || 'candidate')]">{{ statusLabel(row.status) }}</span><span v-if="row.target_asset" class="dim" style="font-size: 11px;">目标: {{ row.target_asset.display || row.target_asset.display_key }}</span><small v-if="row.retests_count" class="dim">（{{ row.retests_count }} 次复测）</small></div><small v-if="row.disposition_reason" class="dim" style="display: block; margin-top: 4px;">{{ row.disposition_reason }}</small></template><template v-else-if="category === 'js'"><span>{{ jsStatus(jsVersion(row)?.status || 'content_not_imported') }}</span><small v-if="jsVersion(row)?.candidate_count !== undefined">{{ jsVersion(row).candidate_count }} 个候选</small><small v-if="jsVersion(row)?.sha256" class="mono" :title="jsVersion(row).sha256">SHA-256 {{ jsVersion(row).sha256.slice(0, 12) }}…</small><small v-if="jsVersion(row)?.reason" class="warn">{{ jsVersion(row).reason }}</small></template><template v-else-if="category === 'api'"><span class="method-label">{{ apiMethod(row) }}</span> {{ observation(row, ['status','status_code','http.status','http.status_code']) }}<small v-if="observation(row, ['declared_method']) !== '未观察'">声明方法 {{ observation(row, ['declared_method']) }}</small><small v-if="hasJsSource(row)" class="type-label" title="该接口包含来自 JavaScript 源码的静态引用候选">JS 候选</small><small v-if="row.observed_truncated" class="dim">摘要已截断，更多观测见详情</small></template><template v-else>{{ row.lifecycle === 'active' ? '活跃' : '退役' }}</template></td></tr>
          </tbody></table>
        </div>
        <WorkspaceDetail v-if="selected" :key="`${selectedCategory}:${selected.id}`" :row="selected" :category="selectedCategory" @close="selected = null" @select="selectRelated" />
      </div>
      <footer class="workspace-pagination"><span class="dim">当前显示 {{ data.rows.length }} 条 · 第 {{ pageIndex }} 页</span><button :disabled="!history.length" @click="turn(false)">上一页</button><button :disabled="!data.next_cursor" @click="turn(true)">下一页</button></footer>
    </template>

    <!-- 创建人工漏洞发现弹窗 -->
    <div v-if="showCreateModal" class="modal-backdrop" @click.self="showCreateModal = false">
      <div class="modal-dialog">
        <header><h3>＋ 人工录入漏洞发现</h3><button class="ghost" @click="showCreateModal = false">✕</button></header>
        <form class="modal-form" @submit.prevent="submitCreateFinding">
          <label>漏洞标题 *<input v-model="createForm.title" placeholder="例如：管理后台未授权访问" maxlength="200" required /></label>
          <label>目标实体 ID *<input v-model="createForm.target_id" type="number" placeholder="关联主机或接口实体 ID" required /><small class="dim">必须是当前项目已存在的实体 ID（如 host/endpoint）</small></label>
          <label>严重级别<select v-model="createForm.severity"><option value="critical">严重 (critical)</option><option value="high">高危 (high)</option><option value="medium">中危 (medium)</option><option value="low">低危 (low)</option><option value="info">提示 (info)</option></select></label>
          <label>初始处置状态<select v-model="createForm.status"><option value="confirmed">已确认 (confirmed)</option><option value="candidate">待核实 (candidate)</option></select></label>
          <label>判定理由 / 核实依据<textarea v-model="createForm.reason" placeholder="说明该发现如何核实确认…" /></label>
          <label>详细描述与复现细节<textarea v-model="createForm.description" placeholder="漏洞现象、影响接口与复现步骤…" /></label>
          <p v-if="createError" class="error-state" style="margin: 0; font-size: 11px;">{{ createError }}</p>
          <div class="modal-actions">
            <button type="button" class="ghost" @click="showCreateModal = false">取消</button>
            <button type="submit" class="primary" :disabled="createSubmitting">{{ createSubmitting ? '保存中…' : '创建漏洞' }}</button>
          </div>
        </form>
      </div>
    </div>

    <!-- 导出报告弹窗 -->
    <div v-if="showExportModal" class="modal-backdrop" @click.self="showExportModal = false">
      <div class="modal-dialog">
        <header><h3>📄 导出安全研究与漏洞发现报告</h3><button class="ghost" @click="showExportModal = false">✕</button></header>
        <p class="dim" style="font-size: 11px; line-height: 1.6; margin-bottom: 16px;">报告基于本地已导入数据、人工处置判定与复测事实生成，严格恪守单一事实源与脱敏审计纪律。</p>
        <div style="display: flex; gap: 12px; margin-bottom: 20px;">
          <button class="primary" :disabled="exporting" @click="downloadReport('markdown')">下载 Markdown 报告 (.md)</button>
          <button class="secondary" :disabled="exporting" @click="downloadReport('json')">下载结构化 JSON (.json)</button>
        </div>
        <p v-if="exportError" class="error-state" style="margin: 0; font-size: 11px;">{{ exportError }}</p>
        <div class="modal-actions">
          <button type="button" class="ghost" @click="showExportModal = false">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>
