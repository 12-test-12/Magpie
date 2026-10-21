<script setup>
import { onBeforeUnmount, ref, watch } from 'vue'
import { post } from '../api.js'
import { store } from '../store.js'
import { params, requestSlot } from '../workspace.js'
import ResearchTargetPicker from './ResearchTargetPicker.vue'
const props = defineProps({target:{type:Object,required:true}})
const notes = ref(null), tasks = ref(null), error = ref(''), text = ref(''), entity = ref([]), saving = ref(false), message = ref('')
const cursor = ref(null), previous = ref([]), noteSlot = requestSlot(), taskSlot = requestSlot()
let generation = 0
function filters() { return props.target.kind === 'evidence' ? {evidence_id:props.target.id} : {entity_id:props.target.id} }
function load() {
  error.value = ''
  noteSlot.run('/workspace/notes?' + params({project:store.project,...filters(),limit:5,cursor:cursor.value}), r => {notes.value = r}, t => {error.value = t})
  taskSlot.run('/workspace/tasks?' + params({project:store.project,...filters(),limit:5}), r => {tasks.value = r}, t => {error.value = t})
}
function turn(forward) { if (forward) {previous.value.push(cursor.value); cursor.value = notes.value.next_cursor} else cursor.value = previous.value.pop(); load() }
function task() {store.openWorkspace('tasks',{target:props.target},filters())}
async function save() {
  const current = generation, project = store.project, evidence = props.target.kind === 'evidence'
  if (evidence && entity.value.length !== 1) return
  saving.value = true; error.value = ''; message.value = ''
  try {
    await post('/workspace/notes?' + params({project}), {entity_id:evidence ? entity.value[0].id : props.target.id,text:text.value,evidence_ids:evidence ? [props.target.id] : []})
    if (current !== generation) return
    text.value = ''; message.value = '研究笔记已保存'; store.dataRevision++; cursor.value = null; previous.value = []; load()
  } catch (err) {if (current === generation) error.value = err.message}
  finally {if (current === generation) saving.value = false}
}
watch(() => [store.project,props.target.kind,props.target.id], () => {generation++; noteSlot.cancel(); taskSlot.cancel(); text.value = ''; entity.value = []; saving.value = false; notes.value = null; tasks.value = null; cursor.value = null; previous.value = []; message.value = ''; load()}, {immediate:true})
watch(() => store.dataRevision, load)
onBeforeUnmount(() => {generation++; noteSlot.cancel(); taskSlot.cancel()})
</script>
<template><details class="research-materials"><summary>关联研究资料与新建笔记</summary><div v-if="error" role="alert">{{ error }} <button @click="load">重试关联资料</button></div>
  <p v-if="notes">{{ notes.total }} 条活动笔记</p><button v-for="row in notes?.rows || []" :key="row.id" class="material-link ghost" @click="store.openWorkspace('notes',row)">{{ (row.text || '').slice(0,100) }} →</button>
  <div v-if="notes && (previous.length || notes.next_cursor)"><button :disabled="!previous.length" @click="turn(false)">上一页笔记</button><button :disabled="!notes.next_cursor" @click="turn(true)">下一页笔记</button></div>
  <button v-if="tasks" class="ghost" @click="store.openWorkspace('tasks',null,filters())">查看 {{ tasks.total }} 项关联任务 →</button><button class="ghost" @click="task">＋ 为此资料创建研究任务</button>
  <form @submit.prevent="save"><h4>记录研究线索</h4><template v-if="target.kind === 'evidence'"><p class="dim">选择一个资产或 Finding 作为笔记目标；当前证据将作为研究引用。</p><ResearchTargetPicker v-model="entity" /></template><textarea v-model="text" aria-label="研究线索内容" rows="3" maxlength="10000" placeholder="记录观察、依据和仍待核实的问题…" /><button type="submit" :disabled="saving || !text.trim() || target.kind === 'evidence' && entity.length !== 1">保存研究笔记</button></form><p v-if="message" role="status">{{ message }}</p>
</details></template>
<style scoped>.research-materials { margin:20px 0; padding-top:12px; border-top:1px solid var(--line); } .material-link { display:block; text-align:left; white-space:normal; overflow-wrap:anywhere; } textarea { width:100%; box-sizing:border-box; }</style>
