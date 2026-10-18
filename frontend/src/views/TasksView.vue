<script setup>
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { post, put } from '../api.js'
import { store } from '../store.js'
import { params, requestSlot } from '../workspace.js'
import ResearchTargetPicker from '../components/ResearchTargetPicker.vue'

const labels = { pending:'待处理', in_progress:'进行中', done:'已完成', shelved:'已搁置' }
const q = ref(''), status = ref(''), applied = reactive({ q:'', status:'' })
const data = ref(null), error = ref(''), loading = ref(false), cursor = ref(null), history = ref([])
const editing = ref(false), selected = ref(null), saving = ref(false), message = ref(''), writeError = ref('')
const form = reactive({ title:'', description:'', status:'pending', links:[], version:null })
const slot = requestSlot(), detailSlot = requestSlot()
let generation = 0
const page = computed(() => history.value.length + 1)
function load() {
  slot.cancel(); error.value = ''; loading.value = false
  if (!store.project) { data.value = null; return }
  loading.value = true
  slot.run('/workspace/tasks?' + params({project:store.project, ...applied, cursor:cursor.value, limit:50}),
    result => { data.value = result }, text => { error.value = text }, () => { loading.value = false })
}
function filter() { Object.assign(applied, {q:q.value, status:status.value}); cursor.value = null; history.value = []; load() }
function turn(forward) {
  if (forward) { history.value.push(cursor.value); cursor.value = data.value.next_cursor }
  else cursor.value = history.value.pop()
  load()
}
function edit(task = null, target = null) {
  generation++; detailSlot.cancel(); saving.value = false; writeError.value = ''; message.value = ''
  selected.value = task?.id || null; editing.value = true
  Object.assign(form, {title:task?.title || '', description:task?.description || '', status:task?.status || 'pending', links:task?.links ? [...task.links] : target ? [target] : [], version:task?.version ?? null})
}
function reloadTask() {
  if (!selected.value) return
  detailSlot.run('/workspace/tasks/' + selected.value + '?' + params({project:store.project}),
    task => edit(task), text => { writeError.value = text })
}
async function save() {
  const current = generation, project = store.project, id = selected.value
  saving.value = true; writeError.value = ''; message.value = ''
  const payload = {title:form.title, description:form.description, status:form.status, entity_ids:form.links.map(r => r.id), version:form.version}
  try {
    const result = await (id ? put('/workspace/tasks/' + id + '?' + params({project}), payload) : post('/workspace/tasks?' + params({project}), payload))
    if (current !== generation || project !== store.project) return
    selected.value = result.id; Object.assign(form, { ...result, links:result.links || [] })
    message.value = '研究任务已保存'; store.dataRevision++; load()
  } catch (err) { if (current === generation && project === store.project) writeError.value = err.message }
  finally { if (current === generation) saving.value = false }
}
function openTarget(row) {
  if (row.available === false) return
  store.openWorkspace(row.kind === 'vuln' ? 'findings' : row.kind === 'endpoint' ? 'api' : 'assets', row)
}
watch(() => store.project, () => {
  generation++; detailSlot.cancel(); editing.value = false; saving.value = false; selected.value = null
  q.value = ''; status.value = ''; data.value = null; filter()
}, {immediate:true})
watch(() => store.workspaceJump, jump => {
  if (jump?.category !== 'tasks' || jump.project !== store.project) return
  q.value = jump.filters?.q || ''; status.value = jump.filters?.status || ''; filter()
  if (jump.row?.target) edit(null, jump.row.target)
  store.workspaceJump = null
}, {immediate:true})
onBeforeUnmount(() => { generation++; slot.cancel(); detailSlot.cancel() })
</script>
<template>
  <div class="research-page workspace-page">
    <header class="page-heading"><div><div class="eyebrow">RESEARCH TASKS</div><h1>研究任务与待办</h1><p>记录人工研究线索、验证进度和协作依赖。</p></div><button class="primary" :disabled="!store.project" @click="edit()">＋ 新建研究任务</button></header>
    <p class="notice">任务由研究员手动维护。关联对象复用当前项目资产与发现；导入撤销不会删除待办。状态变更不发起网络测试。</p>
    <form class="workspace-filters" @submit.prevent="filter"><label class="search-field">搜索<input v-model="q" aria-label="搜索研究任务" placeholder="标题或描述…" maxlength="300" /></label><label>任务状态<select v-model="status" aria-label="筛选任务状态"><option value="">全部状态</option><option value="open">待处理与进行中</option><option v-for="(label,key) in labels" :key="key" :value="key">{{ label }}</option></select></label><button>应用筛选</button><button type="button" @click="load">刷新</button></form>
    <div class="workspace-body task-body">
      <div class="workspace-results">
        <p v-if="!store.project" class="workspace-state">先创建或选择项目。</p>
        <p v-else-if="loading" role="status" class="workspace-state">正在载入任务…</p>
        <div v-else-if="error" role="alert" class="error-state"><p>{{ error }}</p><button @click="load">重试</button></div>
        <template v-else-if="data"><p class="result-summary">{{ data.total }} 项任务 · 第 {{ page }} 页</p><p v-if="!data.rows.length" class="workspace-state">暂无符合条件的任务，可新建待办或调整筛选。</p>
          <article v-for="task in data.rows" :key="task.id" class="task-card" :class="{selected: selected === task.id}"><header><button class="row-link" @click="edit(task)">{{ task.title }}</button><span class="type-label">{{ labels[task.status] }}</span></header><p>{{ task.description || '尚无补充说明' }}</p><div class="task-links"><button v-for="link in task.links" :key="link.id" class="ghost" :disabled="!link.available" @click="openTarget(link)">{{ link.display_key }} {{ link.available ? '↗' : '（已撤销/合并）' }}</button></div><small class="dim">更新于 {{ task.updated_at }} · {{ task.updated_by }}</small></article>
        </template>
      </div>
      <aside v-if="editing" class="workspace-detail task-editor" aria-label="研究任务编辑"><header><strong>{{ selected ? '编辑研究任务' : '新建研究任务' }}</strong><button class="ghost" aria-label="关闭任务编辑" @click="generation++; editing = false">✕</button></header><form class="detail-content" @submit.prevent="save"><label>标题<input v-model="form.title" aria-label="任务标题" required maxlength="300" /></label><label>状态<select v-model="form.status" aria-label="任务状态"><option v-for="(label,key) in labels" :key="key" :value="key">{{ label }} ({{ key }})</option></select></label><label>研究说明<textarea v-model="form.description" aria-label="任务研究说明" rows="5" maxlength="10000" /></label><h3>关联对象</h3><ResearchTargetPicker :key="selected || 'new'" v-model="form.links" /><div v-if="writeError" role="alert" class="error-state"><p>{{ writeError }}</p><button v-if="selected" type="button" @click="reloadTask">重新载入任务</button></div><p v-if="message" role="status">{{ message }}</p><button class="primary" type="submit" :disabled="saving || !form.title.trim()">{{ saving ? '保存中…' : '保存任务' }}</button></form></aside>
    </div>
    <footer v-if="data" class="workspace-pagination"><span class="dim">每页最多 50 项</span><button :disabled="!history.length || loading" @click="turn(false)">上一页</button><button :disabled="!data.next_cursor || loading" @click="turn(true)">下一页</button></footer>
  </div>
</template>
<style scoped>
.task-card { padding:18px; border-bottom:1px solid var(--line); }
.task-card header { display:flex; align-items:center; gap:12px; }
.task-card .row-link { flex:1; text-align:left; white-space:normal; overflow-wrap:anywhere; font-weight:600; }
.task-card p { white-space:pre-wrap; overflow-wrap:anywhere; color:var(--dim); }
.task-card.selected { background:var(--tint-active); }
.task-links { display:flex; flex-wrap:wrap; margin-bottom:8px; }
.task-links button { white-space:normal; overflow-wrap:anywhere; text-align:left; max-width:100%; }
.task-editor label { display:flex; flex-direction:column; gap:6px; margin:12px 0; }
.task-editor textarea { resize:vertical; }
.task-editor .detail-content { flex:1; }
</style>
