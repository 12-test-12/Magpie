<script setup>
import { onBeforeUnmount, ref, watch } from 'vue'
import { store } from '../store.js'
import { params, requestSlot } from '../workspace.js'
const since = ref('last'), applied = ref('last'), data = ref(null), error = ref(''), loading = ref(false), cursor = ref(null), previous = ref([])
const slot = requestSlot()
function load() {error.value = ''; data.value = null; if (!store.project) return; loading.value = true; slot.run('/diff?' + params({project:store.project,since:applied.value,cursor:cursor.value,limit:50}),r => {data.value = r},t => {error.value = t},() => {loading.value = false})}
function filter() {applied.value = since.value.trim(); cursor.value = null; previous.value = []; load()}
function turn(forward) {if (forward) {previous.value.push(cursor.value); cursor.value = data.value.next_cursor} else cursor.value = previous.value.pop(); load()}
function open(row) {store.openWorkspace(row.kind === 'endpoint' ? 'api' : row.kind === 'vuln' ? 'findings' : row.kind === 'service' || row.kind === 'product' ? 'services' : 'assets',row)}
watch(() => store.project,filter,{immediate:true})
watch(() => store.dataRevision,filter)
onBeforeUnmount(() => slot.cancel())
</script>
<template><div class="research-page workspace-page"><header class="page-heading"><div><div class="eyebrow">PROJECT CHANGES</div><h1>增量变化</h1><p>按作业归属回查当前有效贡献带来的新增与变化。</p></div></header><form class="workspace-filters" @submit.prevent="filter"><label>起始基准<input v-model="since" aria-label="增量起始基准" placeholder="last / 作业 ID / ISO 时间" required maxlength="100" /></label><button>查询变化</button></form><p class="notice">撤销的贡献不计入结果；时间使用作业开始时间，不冒充目标事实发生时间。相同秒开始的作业属于同一时间窗口。</p><p v-if="loading" role="status">正在查询变化…</p><div v-if="error" role="alert">{{ error }}<button @click="filter">重新查询</button></div><template v-if="data"><p class="result-summary">新增 {{ data.counts.new }} · 变化 {{ data.counts.changed }} · 有效贡献 {{ data.counts.facts }}</p><div class="workspace-results"><p v-if="!data.rows.length">当前基准没有有效变化。</p><button v-for="row in data.rows" :key="row.id" class="change-row ghost" @click="open(row)"><span class="type-label">{{ row.is_new ? '新增' : '变化' }} · {{ row.kind }}</span> {{ row.display_key }}<small>{{ row.fact_count }} 条有效贡献 →</small></button></div><footer class="workspace-pagination"><span>第 {{ previous.length + 1 }} 页 · 每页最多 50 项</span><button :disabled="!previous.length || loading" @click="turn(false)">上一页</button><button :disabled="!data.next_cursor || loading" @click="turn(true)">下一页</button></footer></template></div></template>
<style scoped>.change-row { display:block; width:100%; padding:16px; text-align:left; white-space:normal; overflow-wrap:anywhere; border-bottom:1px solid var(--line); } small { display:block; margin-top:6px; }</style>
