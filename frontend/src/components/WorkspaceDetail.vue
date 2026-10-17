<script setup>
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { get, post } from '../api.js'
import { store } from '../store.js'
import { params, requestSlot } from '../workspace.js'

const props = defineProps({ row: { type: Object, required: true }, category: { type: String, required: true } })
const emit = defineEmits(['close', 'select'])
const section = ref('observations'), detail = ref(null), error = ref(''), loading = ref(false)
const cursor = ref(null), previous = ref([]), evidence = ref(null), note = ref(''), saving = ref(false), message = ref('')
const slot = requestSlot(), previewSlot = requestSlot()
const isEvidence = computed(() => props.row.kind === 'evidence')
const isApiEndpoint = computed(() => props.category === 'api' && props.row.kind === 'endpoint')
const isVuln = computed(() => props.category === 'findings' || props.row.kind === 'vuln')
const entityId = computed(() => props.row.entity_id || props.row.id)
const title = computed(() => detail.value?.entity?.display_key || props.row.display_key || detail.value?.entity?.entity_key || props.row.entity_key || `对象 ${entityId.value}`)
const detailTabs = computed(() => isApiEndpoint.value
  ? [['samples', '请求样本'], ['parameters', '参数结构'], ['js_cross', 'JS 交叉来源'], ['observations', '全部观测'], ['relations', '关联'], ['evidence', '来源证据']]
  : props.category === 'js' && props.row.kind === 'endpoint'
    ? [['js', 'JS 内容与候选'], ['observations', '全部观测'], ['relations', '关联'], ['evidence', '来源证据']]
    : isVuln.value
      ? [['triage', '处置判定'], ['retests', '人工复测'], ['observations', '观测'], ['relations', '关联'], ['evidence', '来源证据']]
      : [['observations', '观测'], ['relations', '关联'], ['evidence', '来源证据']])

const findingDetail = ref(null)
const triageForm = reactive({ status: 'candidate', severity: 'medium', reason: '', duplicate_of_id: null })
const triageSubmitting = ref(false), triageMessage = ref('')
const retestForm = reactive({ status: 'still_vulnerable', notes: '', evidence_id: null })
const retestSubmitting = ref(false), retestMessage = ref('')

async function loadFinding() {
  if (!isVuln.value) return
  try {
    const res = await get('/workspace/findings/' + entityId.value + '?' + params({ project: store.project }))
    findingDetail.value = res
    triageForm.status = res.triage?.status || 'candidate'
    triageForm.severity = res.triage?.severity || 'medium'
    triageForm.reason = res.triage?.disposition_reason || ''
    triageForm.duplicate_of_id = res.triage?.duplicate_of_id || null
  } catch (err) {
    // fallback
  }
}

async function submitTriage() {
  triageSubmitting.value = true; triageMessage.value = ''
  try {
    await post('/workspace/findings/' + entityId.value + '/triage?' + params({ project: store.project }), {
      status: triageForm.status,
      severity: triageForm.severity,
      reason: triageForm.reason.trim() || undefined,
      duplicate_of_id: triageForm.status === 'duplicate' && triageForm.duplicate_of_id ? Number(triageForm.duplicate_of_id) : undefined,
    })
    triageMessage.value = '处置判定已更新并计入审计'
    await loadFinding()
    store.dataRevision++
  } catch (err) {
    triageMessage.value = err.message
  } finally {
    triageSubmitting.value = false
  }
}

async function submitRetest() {
  retestSubmitting.value = true; retestMessage.value = ''
  if (!retestForm.notes.trim()) {
    retestMessage.value = '复测记录说明不能为空'
    retestSubmitting.value = false
    return
  }
  try {
    await post('/workspace/findings/' + entityId.value + '/retest?' + params({ project: store.project }), {
      status: retestForm.status,
      notes: retestForm.notes.trim(),
      evidence_id: retestForm.evidence_id ? Number(retestForm.evidence_id) : undefined,
    })
    retestMessage.value = '复测记录已添加并联动处置状态'
    retestForm.notes = ''
    retestForm.evidence_id = null
    await loadFinding()
    store.dataRevision++
  } catch (err) {
    retestMessage.value = err.message
  } finally {
    retestSubmitting.value = false
  }
}
function objectValue(value) { try { return JSON.parse(value) } catch { return null } }
function observationLabel(field, value) {
  if (field === 'js_static_param') return 'JS 静态参数名候选'
  if (!field.startsWith('param.')) return field
  const isLocation = ['query','path','header','cookie','body','unknown'].includes(String(value).toLowerCase())
  const attr = !isLocation && (field.endsWith('.type') ? '类型属性' : field.endsWith('.required') ? '必填属性' : '')
  return `${attr ? attr + ' · ' : '参数 '}${field.slice(6)}`
}
function observationHint(field, value) {
  if (field === 'js_static_param') return `候选参数名：${value} · 来源为静态 JS 分析，不代表实际请求`
  if (field.startsWith('param.') && field.endsWith('.type') && !['query','path','header','cookie','body','unknown'].includes(String(value).toLowerCase())) return `类型：${value}`
  if (field.startsWith('param.') && field.endsWith('.required') && ['true','false','0','1'].includes(String(value).toLowerCase())) return `契约声明必填：${value}`
  if (value === '[未在参数结构页展示]') return '包含非结构性文本；此页面不展示参数值或说明文本'
  if (field.startsWith('param.')) return `位置：${value || '未知'} · 仅显示名称、类型与位置，不展示参数值`
  return null
}
function credentialLabel(value) {
  return ({ cookie: '观察到 Cookie', bearer: '观察到 Bearer', basic: '观察到 Basic', api_key: '观察到 API key',
    other: '观察到其他凭据', unknown: '凭据状态未知', no_recognized_credentials_observed: '未观察到已识别凭据' })[value] || value
}
function jsStatus(value) {
  return ({ bounded_rules_complete: '有限规则已完成', partial: '部分内容未理解', limit_exceeded: '超过内容上限',
    content_not_imported: '内容未导入', base64_invalid: 'HAR base64 解码失败', invalid_metadata: '版本信息无效' })[value] || value || '状态未知'
}

function openEvidence(id) {
  evidence.value = null; error.value = ''; loading.value = true
  previewSlot.run('/workspace/evidence/' + id + '?' + params({ project: store.project }),
    result => { evidence.value = result }, text => { error.value = text }, () => { loading.value = false })
}
function load() {
  error.value = ''; detail.value = null; evidence.value = null; loading.value = true
  if (isEvidence.value) { openEvidence(props.row.id); return }
  slot.run('/workspace/entities/' + entityId.value + '?' + params({ project: store.project, section: section.value, cursor: cursor.value, limit: 20 }),
    result => { detail.value = result }, text => { error.value = text }, () => { loading.value = false })
}
function tab(key) { section.value = key; previous.value = []; cursor.value = null; previewSlot.cancel(); load() }
function turn(forward) {
  if (forward) { previous.value.push(cursor.value); cursor.value = detail.value.next_cursor }
  else cursor.value = previous.value.pop()
  load()
}
async function copy() {
  try { await navigator.clipboard.writeText(title.value); message.value = '已复制' }
  catch { message.value = '复制不可用，可选中标题手动复制。' }
}
async function saveNote() {
  const project = store.project, id = entityId.value
  saving.value = true; message.value = ''
  try {
    await post('/notes?' + params({ project }), { entity_id: id, text: note.value })
    if (project !== store.project || id !== entityId.value) return
    note.value = ''; message.value = '已通过现有发现通道保存，可在笔记页回查。'; tab('observations')
  } catch (err) { message.value = err.message }
  finally { saving.value = false }
}
watch(() => [store.project, props.row.id, props.row.kind, props.category], () => {
  section.value = isApiEndpoint.value ? 'samples' : props.category === 'js' && props.row.kind === 'endpoint' ? 'js' : isVuln.value ? 'triage' : 'observations'
  slot.cancel(); previewSlot.cancel(); cursor.value = null; previous.value = []; load()
  if (isVuln.value) loadFinding()
}, { immediate: true })
onBeforeUnmount(() => { slot.cancel(); previewSlot.cancel() })
</script>

<template>
  <aside class="workspace-detail" aria-label="研究对象详情">
    <header><span class="eyebrow">{{ isEvidence ? 'EVIDENCE DETAIL' : 'OBJECT DETAIL' }}</span><button class="ghost" aria-label="关闭详情" @click="emit('close')">✕</button></header>
    <h2 class="mono">{{ title }}</h2><div class="detail-actions"><span class="type-label">{{ props.row.kind }}</span><button class="ghost" @click="copy">复制名称</button></div>
    <nav v-if="!isEvidence" class="detail-tabs"><button v-for="[key,label] in detailTabs" :key="key" :class="{ active: section === key }" @click="tab(key)">{{ label }}</button></nav>
    <div class="detail-content">
      <p v-if="detail?.entity?.identity_notice" class="notice">{{ detail.entity.identity_notice }}</p>
      <p v-else-if="props.row.identity_status === 'legacy_folded'" class="notice">历史身份按路径模板折叠；确切路径、方法及每次请求组合无法从现存事实完整恢复。</p>
      <section v-if="detail?.entity?.merge_groups?.length" class="merge-identities"><h3>当前有效合并组</h3><div v-for="group in detail.entity.merge_groups" :key="group.event_id"><p>合并事件 #{{ group.event_id }} · {{ group.reason }}</p><ul><li v-for="member in group.members" :key="member.entity_id"><span class="mono">{{ member.display_key }}</span><small v-if="member.identity_status === 'legacy_folded'" class="warn">历史折叠身份</small></li></ul><p v-if="group.members_truncated || detail.entity.merge_groups_truncated" class="warn">合并成员列表有上限，当前仅展示部分成员。</p></div></section>
      <p v-if="loading" role="status" class="dim">正在载入详情…</p>
      <div v-if="error" role="alert" class="error-state"><p>{{ error }}</p><button @click="load">重试</button></div>
      <section v-if="evidence" class="evidence-preview">
        <button v-if="!isEvidence" class="ghost" @click="evidence = null">← 返回来源列表</button>
        <h3>原始证据 #{{ evidence.id }}</h3><dl><dt>文件定位</dt><dd class="mono">{{ evidence.locator_uri }}</dd><dt>片段定位</dt><dd class="mono">{{ evidence.locator || '定位已不可用' }}</dd><dt>片段 SHA-256</dt><dd class="mono">{{ evidence.sha256 }}</dd><dt>原文持有 / 恢复状态</dt><dd>{{ evidence.storage_state }} / {{ evidence.recovery_state }}</dd></dl>
        <p class="notice">{{ evidence.preview_reason }}</p><pre v-if="evidence.preview !== null">{{ evidence.preview }}</pre><p v-if="evidence.preview_truncated" class="warn">预览已截断，仅展示前 8,000 字符。</p>
        <h3>有效贡献链</h3><p v-if="!evidence.contributions?.length" class="dim">当前没有有效贡献；可能属于已撤销的历史证据。</p><p v-for="item in evidence.contributions || []" :key="item.id" class="mono">{{ item.source }} · job #{{ item.job_id }} · {{ item.subject_kind }} #{{ item.subject_id }}</p><p v-if="evidence.contributions_truncated" class="warn">仅列出前 50 条有效贡献。</p>
      </section>

      <section v-else-if="section === 'triage'" class="triage-section">
        <div class="triage-box">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
            <strong>处置研判状态</strong>
            <span :class="['badge-status', 'status-' + (findingDetail?.triage?.status || 'candidate')]">
              {{ findingDetail?.triage?.status || 'candidate' }}
            </span>
          </div>
          <p v-if="findingDetail?.title"><strong>漏洞标题</strong>：{{ findingDetail.title }}</p>
          <p v-if="findingDetail?.target_asset">
            <strong>影响目标</strong>：
            <button class="ghost inline-link" @click="emit('select', { id: findingDetail.target_asset.id, kind: findingDetail.target_asset.kind })">
              {{ findingDetail.target_asset.display_key }} ↗
            </button>
          </p>
          <p v-if="findingDetail?.triage?.disposition_reason"><strong>研判依据</strong>：{{ findingDetail.triage.disposition_reason }}</p>
          <p v-if="findingDetail?.triage?.duplicate_of_id">
            <strong>主漏洞</strong>：
            <button class="ghost inline-link" @click="emit('select', { id: findingDetail.triage.duplicate_of_id, kind: 'vuln' })">
              #{{ findingDetail.triage.duplicate_of_id }} {{ findingDetail.triage.duplicate_of_display || '' }} ↗
            </button>
          </p>
          <small v-if="findingDetail?.triage?.updated_at" class="dim">最后更新：{{ findingDetail.triage.updated_at }} · {{ findingDetail.triage.updated_by || 'analyst' }}</small>
        </div>

        <form class="modal-form" @submit.prevent="submitTriage" style="background:#19222a; padding:12px; border-radius:8px; border:1px solid #2b3941;">
          <h3 style="margin:0 0 8px; font-size:12px;">更新处置决策</h3>
          <label>处置状态
            <select v-model="triageForm.status">
              <option value="confirmed">已确认 (confirmed)</option>
              <option value="candidate">待核实 (candidate)</option>
              <option value="resolved">已修复 (resolved)</option>
              <option value="false_positive">误报排除 (false_positive)</option>
              <option value="duplicate">重复发现 (duplicate)</option>
              <option value="inconclusive">存疑 (inconclusive)</option>
            </select>
          </label>
          <label>严重级别
            <select v-model="triageForm.severity">
              <option value="critical">严重 (critical)</option>
              <option value="high">高危 (high)</option>
              <option value="medium">中危 (medium)</option>
              <option value="low">低危 (low)</option>
              <option value="info">提示 (info)</option>
            </select>
          </label>
          <label v-if="triageForm.status === 'duplicate'">主漏洞 ID
            <input v-model="triageForm.duplicate_of_id" type="number" placeholder="主漏洞实体 ID" required />
          </label>
          <label>判定理由 / 核实说明
            <textarea v-model="triageForm.reason" placeholder="记录处置决策依据…" rows="3" />
          </label>
          <button type="submit" class="primary" :disabled="triageSubmitting" style="margin-top:6px;">
            {{ triageSubmitting ? '保存中…' : '更新处置状态' }}
          </button>
          <p v-if="triageMessage" :class="{ 'error-state': triageMessage.includes('失败') || triageMessage.includes('error'), dim: !triageMessage.includes('失败') }" style="margin:6px 0 0; font-size:11px;">
            {{ triageMessage }}
          </p>
        </form>
      </section>

      <section v-else-if="section === 'retests'" class="retest-section">
        <div class="retest-warn">
          ⚠️ 网络超时、无法连接或登录失效严禁判定为已修复（resolved）。复测必须基于明确的重现与修复验证证据。
        </div>

        <div v-if="findingDetail?.retests?.length" class="retests-history">
          <div v-for="rt in findingDetail.retests" :key="rt.id" class="retest-item">
            <div class="retest-item-header">
              <span :class="['badge-status', 'status-' + rt.status]">{{ rt.status }}</span>
              <span class="dim">{{ rt.created_at }} · {{ rt.actor }}</span>
            </div>
            <p style="margin:4px 0; font-size:11px; white-space:pre-wrap;">{{ rt.notes }}</p>
            <div v-if="rt.evidence_id">
              <button class="ghost inline-link" @click="openEvidence(rt.evidence_id)">查看复测关联证据 #{{ rt.evidence_id }} →</button>
            </div>
          </div>
        </div>
        <p v-else class="dim" style="margin-bottom:14px;">暂无人工复测记录。</p>

        <form class="modal-form" @submit.prevent="submitRetest" style="background:#19222a; padding:12px; border-radius:8px; border:1px solid #2b3941;">
          <h3 style="margin:0 0 8px; font-size:12px;">＋ 添加人工复测记录</h3>
          <label>复测结论 *
            <select v-model="retestForm.status">
              <option value="still_vulnerable">漏洞仍存在 (still_vulnerable)</option>
              <option value="resolved">已修复验证 (resolved)</option>
              <option value="inconclusive">存疑/无法下结论 (inconclusive)</option>
            </select>
          </label>
          <label>复测说明与现象 *
            <textarea v-model="retestForm.notes" placeholder="记录复测操作、响应状态及验证依据…" rows="3" required />
          </label>
          <label>关联复测证据 ID
            <input v-model="retestForm.evidence_id" type="number" placeholder="证据 ID (可选)" />
          </label>
          <button type="submit" class="primary" :disabled="retestSubmitting" style="margin-top:6px;">
            {{ retestSubmitting ? '保存中…' : '提交复测记录' }}
          </button>
          <p v-if="retestMessage" :class="{ 'error-state': retestMessage.includes('失败') || retestMessage.includes('空'), dim: !retestMessage.includes('失败') }" style="margin:6px 0 0; font-size:11px;">
            {{ retestMessage }}
          </p>
        </form>
      </section>

      <template v-else-if="detail && !loading && !error">
        <p class="dim">{{ detail.total }} 条{{ section === 'samples' ? 'HTTP 请求样本' : section === 'parameters' ? '参数名记录' : section === 'js_cross' ? 'JS 交叉来源记录' : section === 'observations' ? '有效观测' : section === 'relations' ? '有效关系' : section === 'evidence' ? '有效来源证据' : 'JS 内容记录' }} · 当前页 {{ detail.rows.length }} 条</p>
        <p v-if="isApiEndpoint && section === 'samples' && !detail.total" class="notice">当前没有已导入并关联到此接口的 HTTP 请求样本，凭据状态未知。契约声明或静态 JS 候选不表示实际发送；历史折叠身份的逐请求样本也无法从旧事实恢复。</p>
        <p v-if="!detail.rows.length" class="dim">{{ section === 'js' ? '内容未导入，或没有可展示的内容记录。' : '没有此类记录。缺少观测不代表目标不存在或没有风险。' }}</p>
        <dl v-if="['observations','js','samples','parameters','js_cross'].includes(section)" class="observation-list"><template v-for="item in detail.rows" :key="item.id"><dt>{{ observationLabel(item.semantic_field, item.value) }}<small>{{ item.confidence }} · {{ item.source_count }} 个来源</small></dt><dd v-if="item.semantic_field === 'http_request_sample' && objectValue(item.value)"><strong>HTTP 请求样本 · {{ objectValue(item.value).method || '方法未知' }} · {{ objectValue(item.value).status ?? '状态未知' }}</strong><p class="mono">{{ objectValue(item.value).url }}<span v-if="objectValue(item.value).url_truncated">（路径/地址已截断）</span></p><p>查询参数名：{{ objectValue(item.value).query_parameter_names?.join('、') || '未观察' }}</p><p>请求体参数名：{{ objectValue(item.value).body_parameter_names?.join('、') || '未观察' }} · {{ objectValue(item.value).body_parameters_state }}</p><p>请求头名称：{{ objectValue(item.value).request_header_names?.join('、') || '未提供' }} · {{ objectValue(item.value).request_headers_state }}<span v-if="objectValue(item.value).request_header_names_truncated">（名称列表截断，共 {{ objectValue(item.value).request_header_count }} 个）</span></p><p>凭据线索：{{ objectValue(item.value).credentials?.map(credentialLabel).join('、') || '未知' }}</p><p>响应：{{ objectValue(item.value).response_mime || 'MIME 未知' }} · {{ objectValue(item.value).response_size ?? '大小未知' }} bytes · {{ objectValue(item.value).record_path }}</p><p v-if="objectValue(item.value).sample_truncated" class="warn">样本字段受上限截断；完整原文仍按来源定位检查。</p><p v-if="item.source" class="dim">来源：{{ item.source }}</p><button v-if="item.evidence_id" class="ghost" @click="openEvidence(item.evidence_id)">打开此样本的来源证据 →</button></dd><dd v-else-if="item.semantic_field.startsWith('param.') || item.semantic_field === 'js_static_param'">{{ observationHint(item.semantic_field, item.value) }}</dd><dd v-else-if="item.semantic_field === 'declared_method'"><strong>契约声明的方法</strong><p>{{ objectValue(item.value)?.join('、') || item.value }}</p></dd><dd v-else-if="item.semantic_field === 'js_content_version' && objectValue(item.value)"><strong>内容版本 · {{ jsStatus(objectValue(item.value).status) }}</strong><p class="mono">SHA-256 {{ objectValue(item.value).sha256 }}</p><p v-if="objectValue(item.value).candidate_count !== undefined">{{ objectValue(item.value).candidate_count }} 个候选 · {{ objectValue(item.value).unsupported_count }} 个未支持/不确定调用</p><p>规则 {{ objectValue(item.value).rule_id }}@{{ objectValue(item.value).rule_version }} · {{ objectValue(item.value).reason || '仅表示本规则集处理完成，不代表完整理解 JavaScript。' }}</p><small>{{ objectValue(item.value).record_path }}</small></dd><dd v-else-if="item.semantic_field === 'js_candidate' && objectValue(item.value)"><strong>静态代码引用候选</strong><p class="mono">{{ objectValue(item.value).method || '方法未知' }} {{ objectValue(item.value).url }}</p><p>规则 {{ objectValue(item.value).rule_id }}@{{ objectValue(item.value).rule_version }} · 解码正文字符 {{ objectValue(item.value).start }}–{{ objectValue(item.value).end }} · 第 {{ objectValue(item.value).line }} 行</p><pre>{{ objectValue(item.value).snippet }}</pre><p>{{ objectValue(item.value).reason || '绝对 URL 候选；不表示实际发送' }}</p><p>{{ objectValue(item.value).http_observed ? '已观察到该精确身份的 HTTP 方法/请求样本' : '尚无该精确身份的 HTTP 方法/请求样本；目前仅为静态候选' }}</p><button v-if="objectValue(item.value).target_id" class="ghost" @click="emit('select', { id: objectValue(item.value).target_id, category: 'api', kind: 'endpoint' })">打开关联 API 详情 →</button><span v-else class="dim">接口目标尚未进入有效实体</span><small class="mono">内容版本 SHA-256 {{ objectValue(item.value).content_sha256 }} · {{ objectValue(item.value).record_path }}（字符偏移针对解码正文）</small></dd><dd v-else-if="item.semantic_field === 'js_source_ref' && objectValue(item.value)"><strong>JS 来源引用</strong><p class="mono">{{ objectValue(item.value).resource_display || objectValue(item.value).resource_key || 'JS 资源' }}</p><small>内容版本 SHA-256 {{ objectValue(item.value).content_sha256 }} · {{ objectValue(item.value).record_path }}</small><p><button v-if="objectValue(item.value).resource_id" class="ghost" @click="emit('select', { id: objectValue(item.value).resource_id, category: 'js', kind: 'endpoint' })">打开 JS 来源版本 →</button></p></dd><dd v-else-if="item.semantic_field === 'js_content_status'">{{ jsStatus(item.value) }}</dd><dd v-else>{{ item.value }}<span v-if="item.value_truncated" class="warn">（仅显示前 8,000 字符）</span></dd></template></dl>
        <div v-else-if="section === 'relations'" class="relation-list"><button v-for="item in detail.rows" :key="item.id" @click="emit('select', item.other_id)"><span class="dim">{{ item.src_id === entityId ? '→' : '←' }} {{ item.rel }}</span><strong class="mono">{{ item.other_display || item.other_key || item.other_id }}</strong></button></div>
        <div v-else class="relation-list"><button v-for="item in detail.rows" :key="item.id" @click="openEvidence(item.id)"><span class="dim">证据 #{{ item.id }} · {{ item.storage_state }}</span><strong class="mono">{{ item.entity_key }}</strong><small>{{ item.locator }}</small></button></div>
        <div v-if="previous.length || detail.next_cursor" class="detail-pagination"><button :disabled="!previous.length" @click="turn(false)">上一页</button><button :disabled="!detail.next_cursor" @click="turn(true)">下一页</button></div>
      </template>
      <form v-if="!isEvidence" class="note-form" @submit.prevent="saveNote"><h3>记录研究线索</h3><p class="dim">写入既有 AI 发现通道，尚无独立笔记状态或复测生命周期。</p><textarea v-model="note" aria-label="研究线索内容" placeholder="记录观察、依据和仍待核实的问题…" maxlength="10000" rows="3" /><button :disabled="!note.trim() || saving" type="submit">{{ saving ? '保存中…' : '保存笔记' }}</button></form>
      <p v-if="message" role="status">{{ message }}</p>
    </div>
  </aside>
</template>
