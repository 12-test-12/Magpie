<script setup>
import { onBeforeUnmount, reactive, ref, watch } from 'vue'
import { patch } from '../api.js'
import { store } from '../store.js'
import { params, requestSlot } from '../workspace.js'
import ConflictResolver from './ConflictResolver.vue'
import { mergeFields } from '../merge.js'
const props = defineProps({entityId:{type:Number,required:true}})
const data = ref(null), error = ref(''), message = ref(''), saving = ref(false), conflict = ref(false)
const form = reactive({name:'',status:'',reason:''}), slot = requestSlot()
const loading = ref(false), reconciliation = ref(null)
const statusLabels = {'':'未设置',unreviewed:'未审核',reviewed:'已审核',needs_review:'待复核'}
const values = record => ({name:record.display_name || '',status:record.review_status || ''})
let generation = 0
function load() { error.value = ''; loading.value = true; reconciliation.value = null; slot.run('/workspace/entities/' + props.entityId + '/values?' + params({project:store.project}), result => {data.value = result; Object.assign(form,{...values(result),reason:''}); conflict.value = false}, text => {error.value = text}, () => {loading.value = false}) }
function compareLatest() {
  error.value = ''; loading.value = true
  slot.run('/workspace/entities/' + props.entityId + '/values?' + params({project:store.project}), result => {
    reconciliation.value = {latest:result,fields:mergeFields([
      {key:'name',label:'人工名称'}, {key:'status',label:'人工审核状态',format:value => statusLabels[value]},
    ], values(data.value), values(result), {name:form.name.trim(),status:form.status})}
  }, text => {error.value = text}, () => {loading.value = false})
}
function applyMerge(merged) {
  data.value = reconciliation.value.latest; Object.assign(form,merged)
  reconciliation.value = null; conflict.value = false; error.value = ''
  message.value = `已按选择准备版本 ${data.value.version} 的草稿，请检查后手动保存。`
}
async function save() {
  if (!data.value || conflict.value || saving.value || loading.value || reconciliation.value) return
  const current = generation, project = store.project
  saving.value = true; error.value = ''; message.value = ''
  try {
    const result = await patch('/entities/' + props.entityId + '?' + params({project}), {display_name:form.name.trim() || null,review_status:form.status || null,expected_version:data.value.version,reason:form.reason})
    if (current !== generation) return
    data.value = result.manual_values; form.reason = ''; message.value = '人工字段已保存，原始身份与观测保留。'; store.dataRevision++
  } catch (err) {if (current === generation) {error.value = err.message; conflict.value = err.code === 'E_STALE_RESEARCH'}}
  finally {if (current === generation) saving.value = false}
}
watch(() => [store.project,props.entityId], () => {generation++; slot.cancel(); data.value = null; conflict.value = false; saving.value = false; message.value = ''; load()}, {immediate:true})
onBeforeUnmount(() => {generation++; slot.cancel()})
</script>
<template><details class="manual-values"><summary>人工名称与审核状态</summary><p class="dim">原始身份和解析观测继续保留。此审核状态用于研究进度，不改变 Finding 判定。</p><p v-if="loading" role="status">正在读取人工字段…</p><form v-if="data" @submit.prevent="save"><fieldset :disabled="loading || saving || !!reconciliation"><label>人工名称<input v-model="form.name" aria-label="人工名称" maxlength="300" /></label><label>人工审核状态<select v-model="form.status" aria-label="人工审核状态"><option value="">未设置</option><option value="unreviewed">未审核</option><option value="reviewed">已审核</option><option value="needs_review">待复核</option></select></label><label>修改理由<input v-model="form.reason" aria-label="人工字段修改理由" required maxlength="2000" /></label><p>版本 {{ data.version }} · {{ data.updated_by || '尚无人工值' }}</p><button :disabled="loading || saving || conflict || !!reconciliation || !form.reason.trim()">保存人工字段</button></fieldset></form><div v-if="error || conflict" role="alert">{{ error }}<button type="button" :disabled="loading || saving" @click="load">重新载入人工字段</button><button v-if="conflict" type="button" :disabled="loading || !!reconciliation" @click="compareLatest">核对最新值与草稿</button><p v-if="conflict">草稿保留；核对后逐项选择，重新载入会替换编辑区。</p></div><ConflictResolver v-if="reconciliation" :fields="reconciliation.fields" :version="reconciliation.latest.version" @apply="applyMerge" @cancel="reconciliation = null" /><p v-if="message" role="status">{{ message }}</p></details></template>
<style scoped>fieldset { border:0; padding:0; margin:0; min-width:0; } details { margin:16px 0; } label { display:block; margin:8px 0; } input:not([type=checkbox]), select { display:block; width:100%; box-sizing:border-box; }</style>
