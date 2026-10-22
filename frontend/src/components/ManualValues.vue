<script setup>
import { onBeforeUnmount, reactive, ref, watch } from 'vue'
import { patch } from '../api.js'
import { store } from '../store.js'
import { params, requestSlot } from '../workspace.js'
const props = defineProps({entityId:{type:Number,required:true}})
const data = ref(null), error = ref(''), message = ref(''), saving = ref(false), conflict = ref(false)
const form = reactive({name:'',status:'',reason:''}), slot = requestSlot()
let generation = 0
function load() { error.value = ''; slot.run('/workspace/entities/' + props.entityId + '/values?' + params({project:store.project}), result => {data.value = result; Object.assign(form,{name:result.display_name || '',status:result.review_status || '',reason:''}); conflict.value = false}, text => {error.value = text}) }
function adoptLatest() {
  error.value = ''
  slot.run('/workspace/entities/' + props.entityId + '/values?' + params({project:store.project}), result => {
    data.value = result; conflict.value = false; message.value = `已同步最新版本 ${result.version}，当前修改已保留，可重新提交。`
  }, text => {error.value = text})
}
async function save() {
  if (!data.value || conflict.value || saving.value) return
  const current = generation, project = store.project
  saving.value = true; error.value = ''; message.value = ''
  try {
    const result = await patch('/entities/' + props.entityId + '?' + params({project}), {display_name:form.name.trim() || null,review_status:form.status || null,expected_version:data.value.version,reason:form.reason})
    if (current !== generation) return
    data.value = result.manual_values; form.reason = ''; message.value = '人工字段已保存，原始身份与观测保留。'; store.dataRevision++
  } catch (err) {if (current === generation) {error.value = err.message; conflict.value = err.code === 'E_STALE_RESEARCH'}}
  finally {if (current === generation) saving.value = false}
}
watch(() => [store.project,props.entityId], () => {generation++; slot.cancel(); data.value = null; saving.value = false; message.value = ''; load()}, {immediate:true})
onBeforeUnmount(() => {generation++; slot.cancel()})
</script>
<template><details class="manual-values"><summary>人工名称与审核状态</summary><p class="dim">原始身份和解析观测继续保留。此审核状态用于研究进度，不改变 Finding 判定。</p><form v-if="data" @submit.prevent="save"><label>人工名称<input v-model="form.name" aria-label="人工名称" maxlength="300" /></label><label>人工审核状态<select v-model="form.status" aria-label="人工审核状态"><option value="">未设置</option><option value="unreviewed">未审核</option><option value="reviewed">已审核</option><option value="needs_review">待复核</option></select></label><label>修改理由<input v-model="form.reason" aria-label="人工字段修改理由" required maxlength="2000" /></label><p>版本 {{ data.version }} · {{ data.updated_by || '尚无人工值' }}</p><button :disabled="saving || conflict || !form.reason.trim()">保存人工字段</button></form><div v-if="error" role="alert">{{ error }}<button type="button" @click="load">重新载入人工字段</button><button v-if="conflict" type="button" @click="adoptLatest">保留草稿并同步最新版本</button><p v-if="conflict">草稿保留；重新载入会替换编辑区。</p></div><p v-if="message" role="status">{{ message }}</p></details></template>
<style scoped>details { margin:16px 0; } label { display:block; margin:8px 0; } input:not([type=checkbox]), select { display:block; width:100%; box-sizing:border-box; }</style>
