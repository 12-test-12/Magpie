<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { store } from '../store.js'
import { params, requestSlot } from '../workspace.js'
const props = defineProps({ modelValue: { type: Array, default: () => [] } })
const emit = defineEmits(['update:modelValue'])
const q = ref(''), data = ref(null), error = ref(''), loading = ref(false)
const slot = requestSlot()
let timer
const targets = computed(() => (data.value?.groups || []).filter(g => ['assets','api','findings'].includes(g.category))
  .flatMap(g => g.rows).filter(row => ['domain','ip','host','endpoint','vuln'].includes(row.kind) && !props.modelValue.some(v => v.id === row.id)))
function search() {
  slot.cancel(); data.value = null; error.value = ''
  if (!q.value.trim()) { loading.value = false; return }
  loading.value = true
  slot.run('/workspace/search?' + params({ project: store.project, q: q.value.trim() }),
    result => { data.value = result }, text => { error.value = text }, () => { loading.value = false })
}
watch(q, () => { clearTimeout(timer); slot.cancel(); data.value = null; loading.value = !!q.value.trim(); timer = setTimeout(search, 250) })
watch(() => store.project, () => { clearTimeout(timer); q.value = ''; search() })
onBeforeUnmount(() => { clearTimeout(timer); slot.cancel() })
</script>
<template>
  <div class="target-picker">
    <div v-for="target in modelValue" :key="target.id" class="target-chip"><span>{{ target.title || target.display_key || target.entity_key }}<small v-if="target.available === false" class="warn"> · 对象已撤销/合并</small></span><button type="button" class="ghost" :aria-label="'移除关联 ' + target.id" @click="emit('update:modelValue', modelValue.filter(v => v.id !== target.id))">×</button></div>
    <input v-model="q" aria-label="搜索关联对象" placeholder="搜索主机、Endpoint 或 Finding（最多关联 20 个）" maxlength="300" />
    <p v-if="loading" class="dim">正在查找对象…</p><div v-else-if="error" role="alert">{{ error }} <button type="button" @click="search">重试</button></div>
    <div v-else-if="data" class="target-results"><button v-for="row in targets" :key="row.id" type="button" :disabled="modelValue.length >= 20" @click="emit('update:modelValue', [...modelValue, row])"><span class="type-label">{{ row.kind }}</span> {{ row.title || row.display_key || row.entity_key }}</button><small class="dim">每类最多展示 5 项；输入更具体的关键词缩小范围。</small></div>
  </div>
</template>
<style scoped>
.target-chip { display:flex; align-items:center; gap:8px; padding:4px; }
.target-chip > span { flex:1; overflow-wrap:anywhere; }
.target-results { max-height:200px; overflow:auto; }
.target-results button { display:block; width:100%; margin:5px 0; text-align:left; overflow-wrap:anywhere; }
</style>
