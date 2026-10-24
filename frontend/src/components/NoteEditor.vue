<script setup>
import { onBeforeUnmount, reactive, ref, watch } from 'vue'
import { put } from '../api.js'
import { store } from '../store.js'
import { params, requestSlot } from '../workspace.js'
import ResearchTargetPicker from './ResearchTargetPicker.vue'
import ConflictResolver from './ConflictResolver.vue'
import { mergeFields } from '../merge.js'
const props = defineProps({ noteId:{type:Number,required:true} })
const emit = defineEmits(['select','evidence'])
const data = ref(null), error = ref(''), message = ref(''), loading = ref(false), saving = ref(false), conflict = ref(false)
const history = ref(null), historyCursor = ref(null), previous = ref([])
const form = reactive({text:'',archived:false,reason:'',refs:[]})
const slot = requestSlot(), historySlot = requestSlot()
const reconciliation = ref(null)
const noteValues = note => ({text:note.text, archived:note.archived, evidence_ids:(note.evidence_links || []).map(r => r.id).sort((a,b) => a-b)})
let generation = 0
function load() {
  loading.value = true; error.value = ''; reconciliation.value = null
  slot.run('/workspace/notes/' + props.noteId + '?' + params({project:store.project}), result => {
    data.value = result; Object.assign(form,{text:result.text,archived:result.archived,reason:'',refs:result.evidence_links || []}); conflict.value = false
  }, text => {error.value = text}, () => {loading.value = false})
}
function compareLatest() {
  loading.value = true; error.value = ''
  slot.run('/workspace/notes/' + props.noteId + '?' + params({project:store.project}), result => {
    if (!result.available) { error.value = '笔记或目标已撤销/合并，不能继续编辑；当前草稿保留。'; return }
    reconciliation.value = {latest:result, fields:mergeFields([
      {key:'text',label:'笔记正文'},
      {key:'archived',label:'归档状态',format:value => value ? '已归档' : '活动笔记'},
      {key:'evidence_ids',label:'关联研究证据',format:ids => ids.length ? ids.map(id => '#' + id).join('、') : '无关联研究证据'},
    ], noteValues(data.value), noteValues(result), noteValues({...form,evidence_links:form.refs}))}
  }, text => {error.value = text}, () => {loading.value = false})
}
function applyMerge(values) {
  const latest = reconciliation.value.latest
  const refs = new Map([...form.refs,...(latest.evidence_links || [])].map(row => [row.id,row]))
  data.value = latest
  Object.assign(form,{text:values.text,archived:values.archived,refs:values.evidence_ids.map(id => refs.get(id))})
  reconciliation.value = null; conflict.value = false; error.value = ''
  message.value = `已按选择准备版本 ${latest.version} 的草稿，请检查后手动保存。`
}
async function copyDraft() {
  try {
    await navigator.clipboard.writeText(form.text)
    message.value = '草稿正文已复制到剪贴板。'
  } catch {
    message.value = '复制不可用，请手动选中编辑区正文复制。'
  }
}
function loadHistory() {
  historySlot.run('/workspace/notes/' + props.noteId + '/history?' + params({project:store.project,cursor:historyCursor.value,limit:20}),
    result => {history.value = result}, text => {error.value = text})
}
function turn(forward) {
  if (forward) { previous.value.push(historyCursor.value); historyCursor.value = history.value.next_cursor }
  else historyCursor.value = previous.value.pop()
  loadHistory()
}
async function save() {
  if (!data.value || saving.value || loading.value || conflict.value || reconciliation.value || !data.value.available) return
  const current = generation, project = store.project, id = props.noteId
  saving.value = true; error.value = ''; message.value = ''
  try {
    const result = await put('/workspace/notes/' + id + '?' + params({project}), {
      text:form.text,archived:form.archived,reason:form.reason,expected_version:data.value.version,evidence_ids:form.refs.map(r => r.id)})
    if (current !== generation) return
    data.value = result; form.reason = ''; message.value = '笔记修订已保存，原始文本与证据保留。'; store.dataRevision++
    historyCursor.value = null; previous.value = []; loadHistory()
  } catch (err) { if (current === generation) {error.value = err.message; conflict.value = err.code === 'E_STALE_RESEARCH'} }
  finally { if (current === generation) saving.value = false }
}
watch(() => [store.project,props.noteId], () => {generation++; slot.cancel(); historySlot.cancel(); data.value = null; history.value = null; conflict.value = false; saving.value = false; message.value = ''; historyCursor.value = null; previous.value = []; load()}, {immediate:true})
onBeforeUnmount(() => {generation++; slot.cancel(); historySlot.cancel()})
</script>
<template>
  <section class="note-editor"><h3>所选笔记 · 研究笔记与人工修订</h3><p v-if="loading" role="status">正在载入笔记…</p>
    <div v-if="error || conflict" role="alert" class="error-state">
      <p v-if="error">{{ error }}</p>
      <div class="conflict-actions">
        <button type="button" :disabled="loading || saving" @click="load">重新载入笔记</button>
        <button v-if="conflict" type="button" class="ghost" @click="copyDraft">复制当前草稿</button>
        <button v-if="conflict" type="button" :disabled="loading || !!reconciliation" @click="compareLatest">核对最新值与草稿</button>
      </div>
      <p v-if="conflict">本次未写入，草稿已保留。核对最新值与草稿后逐项选择，或先复制草稿再重新载入。</p>
    </div>
    <ConflictResolver v-if="reconciliation" :fields="reconciliation.fields" :version="reconciliation.latest.version" @apply="applyMerge" @cancel="reconciliation = null" />
    <template v-if="data"><p>版本 {{ data.version }} · {{ data.archived ? '已归档' : '活动笔记' }}</p>
      <p class="notice">编辑区与历史为脱敏副本；保存追加人工修订，不修改原始笔记。导入撤销后保留历史，不能继续编辑。</p>
      <button type="button" class="ghost" @click="emit('select',{id:data.entity_id,kind:data.target_kind,category:data.target_kind === 'vuln' ? 'findings' : data.target_kind === 'endpoint' ? 'api' : 'assets'})">打开关联对象 →</button>
      <button v-if="data.evidence_id" type="button" class="ghost" @click="emit('evidence',data.evidence_id)">原始笔记证据 #{{ data.evidence_id }}</button>
      <form @submit.prevent="save"><fieldset :disabled="loading || saving || !!reconciliation"><label>笔记正文<textarea v-model="form.text" aria-label="编辑笔记正文" rows="6" required maxlength="10000" /></label>
        <label><input v-model="form.archived" type="checkbox" aria-label="归档笔记" />归档笔记（活动列表与报告默认排除）</label>
        <h4>关联研究证据</h4><ResearchTargetPicker v-model="form.refs" evidence-only />
        <button v-for="ref in form.refs" :key="ref.id" type="button" class="ghost" @click="emit('evidence',ref.id)">查看证据 #{{ ref.id }} →</button>
        <label>修改理由<textarea v-model="form.reason" aria-label="笔记修改理由" required maxlength="2000" rows="2" /></label>
        <button type="submit" :disabled="loading || saving || conflict || !!reconciliation || !data.available || !form.text.trim() || !form.reason.trim()">{{ saving ? '保存中…' : '保存笔记修订' }}</button></fieldset></form>
      <details><summary>原始文本（保留）</summary><pre>{{ data.original_text }}</pre></details>
      <button type="button" class="ghost" @click="loadHistory">查看修订历史</button>
      <template v-if="history"><p>{{ history.total }} 次修订</p><article v-for="item in history.rows" :key="item.version"><strong>版本 {{ item.version }} · {{ item.archived ? '归档' : '活动' }}</strong><p>{{ item.reason }} · {{ item.created_at }} · {{ item.created_by }}</p><pre>{{ item.text }}</pre><small>关联证据 ID：{{ item.evidence_ids }}</small></article><button type="button" :disabled="!previous.length" @click="turn(false)">上一页历史</button><button type="button" :disabled="!history.next_cursor" @click="turn(true)">下一页历史</button></template>
    </template><p v-if="message" role="status">{{ message }}</p>
  </section>
</template>
<style scoped>fieldset { border:0; padding:0; margin:0; min-width:0; } label { display:block; margin:12px 0; } textarea { display:block; width:100%; box-sizing:border-box; } pre { white-space:pre-wrap; overflow-wrap:anywhere; } article { border-top:1px solid var(--line); padding:10px 0; } .conflict-actions { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin:8px 0; }</style>
