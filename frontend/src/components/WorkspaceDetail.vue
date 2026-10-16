<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { post } from '../api.js'
import { store } from '../store.js'
import { params, requestSlot } from '../workspace.js'

const props = defineProps({ row: { type: Object, required: true }, category: { type: String, required: true } })
const emit = defineEmits(['close', 'select'])
const section = ref('observations'), detail = ref(null), error = ref(''), loading = ref(false)
const cursor = ref(null), previous = ref([]), evidence = ref(null), note = ref(''), saving = ref(false), message = ref('')
const slot = requestSlot(), previewSlot = requestSlot()
const isEvidence = computed(() => props.row.kind === 'evidence')
const entityId = computed(() => props.row.entity_id || props.row.id)
const title = computed(() => detail.value?.entity?.entity_key || props.row.entity_key || `对象 ${entityId.value}`)
function objectValue(value) { try { return JSON.parse(value) } catch { return null } }
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
  section.value = props.category === 'js' && props.row.kind === 'endpoint' ? 'js' : 'observations'
  slot.cancel(); previewSlot.cancel(); cursor.value = null; previous.value = []; load()
}, { immediate: true })
onBeforeUnmount(() => { slot.cancel(); previewSlot.cancel() })
</script>

<template>
  <aside class="workspace-detail" aria-label="研究对象详情">
    <header><span class="eyebrow">{{ isEvidence ? 'EVIDENCE DETAIL' : 'OBJECT DETAIL' }}</span><button class="ghost" aria-label="关闭详情" @click="emit('close')">✕</button></header>
    <h2 class="mono">{{ title }}</h2><div class="detail-actions"><span class="type-label">{{ props.row.kind }}</span><button class="ghost" @click="copy">复制名称</button></div>
    <nav v-if="!isEvidence" class="detail-tabs"><button v-for="[key,label] in (props.category === 'js' && props.row.kind === 'endpoint' ? [['js','JS 内容与候选'],['observations','全部观测'],['relations','关联'],['evidence','来源证据']] : [['observations','观测'],['relations','关联'],['evidence','来源证据']])" :key="key" :class="{ active: section === key }" @click="tab(key)">{{ label }}</button></nav>
    <div class="detail-content">
      <p v-if="loading" role="status" class="dim">正在载入详情…</p>
      <div v-if="error" role="alert" class="error-state"><p>{{ error }}</p><button @click="load">重试</button></div>
      <section v-if="evidence" class="evidence-preview">
        <button v-if="!isEvidence" class="ghost" @click="evidence = null">← 返回来源列表</button>
        <h3>原始证据 #{{ evidence.id }}</h3><dl><dt>文件定位</dt><dd class="mono">{{ evidence.locator_uri }}</dd><dt>片段定位</dt><dd class="mono">{{ evidence.locator || '定位已不可用' }}</dd><dt>片段 SHA-256</dt><dd class="mono">{{ evidence.sha256 }}</dd><dt>原文持有 / 恢复状态</dt><dd>{{ evidence.storage_state }} / {{ evidence.recovery_state }}</dd></dl>
        <p class="notice">{{ evidence.preview_reason }}</p><pre v-if="evidence.preview !== null">{{ evidence.preview }}</pre><p v-if="evidence.preview_truncated" class="warn">预览已截断，仅展示前 8,000 字符。</p>
        <h3>有效贡献链</h3><p v-if="!evidence.contributions?.length" class="dim">当前没有有效贡献；可能属于已撤销的历史证据。</p><p v-for="item in evidence.contributions || []" :key="item.id" class="mono">{{ item.source }} · job #{{ item.job_id }} · {{ item.subject_kind }} #{{ item.subject_id }}</p><p v-if="evidence.contributions_truncated" class="warn">仅列出前 50 条有效贡献。</p>
      </section>
      <template v-else-if="detail && !loading && !error">
        <p class="dim">{{ detail.total }} 条{{ section === 'observations' ? '有效观测' : section === 'relations' ? '有效关系' : section === 'evidence' ? '有效来源证据' : 'JS 内容记录' }} · 当前页 {{ detail.rows.length }} 条</p>
        <p v-if="!detail.rows.length" class="dim">{{ section === 'js' ? '内容未导入，或没有可展示的内容记录。' : '没有此类记录。缺少观测不代表目标不存在或没有风险。' }}</p>
        <dl v-if="section === 'observations' || section === 'js'" class="observation-list"><template v-for="item in detail.rows" :key="item.id"><dt>{{ item.semantic_field }}<small>{{ item.confidence }} · {{ item.source_count }} 个来源</small></dt><dd v-if="item.semantic_field === 'js_content_version' && objectValue(item.value)"><strong>内容版本 · {{ jsStatus(objectValue(item.value).status) }}</strong><p class="mono">SHA-256 {{ objectValue(item.value).sha256 }}</p><p v-if="objectValue(item.value).candidate_count !== undefined">{{ objectValue(item.value).candidate_count }} 个候选 · {{ objectValue(item.value).unsupported_count }} 个未支持/不确定调用</p><p>规则 {{ objectValue(item.value).rule_id }}@{{ objectValue(item.value).rule_version }} · {{ objectValue(item.value).reason || '仅表示本规则集处理完成，不代表完整理解 JavaScript。' }}</p><small>{{ objectValue(item.value).record_path }}</small></dd><dd v-else-if="item.semantic_field === 'js_candidate' && objectValue(item.value)"><strong>静态代码引用候选</strong><p class="mono">{{ objectValue(item.value).method || '方法未知' }} {{ objectValue(item.value).url }}</p><p>规则 {{ objectValue(item.value).rule_id }}@{{ objectValue(item.value).rule_version }} · 解码正文字符 {{ objectValue(item.value).start }}–{{ objectValue(item.value).end }} · 第 {{ objectValue(item.value).line }} 行</p><pre>{{ objectValue(item.value).snippet }}</pre><p>{{ objectValue(item.value).reason || '绝对 URL 候选；不表示实际发送' }}</p><button v-if="objectValue(item.value).target_id" class="ghost" @click="emit('select', { id: objectValue(item.value).target_id, category: 'api', kind: 'entity' })">打开关联 API 详情 →</button><span v-else class="dim">接口目标尚未进入有效实体</span><small class="mono">内容版本 SHA-256 {{ objectValue(item.value).content_sha256 }} · {{ objectValue(item.value).record_path }}（字符偏移针对解码正文）</small></dd><dd v-else-if="item.semantic_field === 'js_source_ref' && objectValue(item.value)"><strong>JS 来源引用</strong><p class="mono">{{ objectValue(item.value).resource_key }}</p><small>内容版本 SHA-256 {{ objectValue(item.value).content_sha256 }} · {{ objectValue(item.value).record_path }}</small><p><button v-if="objectValue(item.value).resource_id" class="ghost" @click="emit('select', { id: objectValue(item.value).resource_id, category: 'js', kind: 'endpoint' })">打开 JS 来源版本 →</button></p></dd><dd v-else-if="item.semantic_field === 'js_content_status'">{{ jsStatus(item.value) }}</dd><dd v-else>{{ item.value }}<span v-if="item.value_truncated" class="warn">（仅显示前 8,000 字符）</span></dd></template></dl>
        <div v-else-if="section === 'relations'" class="relation-list"><button v-for="item in detail.rows" :key="item.id" @click="emit('select', item.other_id)"><span class="dim">{{ item.src_id === entityId ? '→' : '←' }} {{ item.rel }}</span><strong class="mono">{{ item.other_key || item.other_id }}</strong></button></div>
        <div v-else class="relation-list"><button v-for="item in detail.rows" :key="item.id" @click="openEvidence(item.id)"><span class="dim">证据 #{{ item.id }} · {{ item.storage_state }}</span><strong class="mono">{{ item.entity_key }}</strong><small>{{ item.locator }}</small></button></div>
        <div v-if="previous.length || detail.next_cursor" class="detail-pagination"><button :disabled="!previous.length" @click="turn(false)">上一页</button><button :disabled="!detail.next_cursor" @click="turn(true)">下一页</button></div>
      </template>
      <form v-if="!isEvidence" class="note-form" @submit.prevent="saveNote"><h3>记录研究线索</h3><p class="dim">写入既有 AI 发现通道，尚无独立笔记状态或复测生命周期。</p><textarea v-model="note" aria-label="研究线索内容" placeholder="记录观察、依据和仍待核实的问题…" maxlength="10000" rows="3" /><button :disabled="!note.trim() || saving" type="submit">{{ saving ? '保存中…' : '保存笔记' }}</button></form>
      <p v-if="message" role="status">{{ message }}</p>
    </div>
  </aside>
</template>
