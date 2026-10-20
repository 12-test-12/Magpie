<script setup>
import { computed, ref, watch } from 'vue'
const props = defineProps({ rows: { type: Array, required: true }, context: { type: String, required: true } })
const emit = defineEmits(['evidence'])
const selected = ref([])
function sample(row) { try { const value = JSON.parse(row.value); return value && typeof value === 'object' && !Array.isArray(value) ? value : {} } catch { return {} } }
function toggle(row) {
  if (selected.value.some(item => item.id === row.id)) selected.value = selected.value.filter(item => item.id !== row.id)
  else if (selected.value.length < 2) selected.value.push({ id:row.id, evidence_id:row.evidence_id, value:sample(row) })
}
const fields = [
  ['started_at','请求时间'], ['status','响应状态码'], ['response_mime','响应 MIME'], ['response_size','响应大小（bytes）'],
  ['query_parameter_names','query 参数名'], ['body_parameter_names','body 参数名'], ['request_header_names','请求头名称'],
  ['credentials','凭据类别'], ['body_parameters_state','请求体解析状态'], ['request_headers_state','请求头完整性'],
]
function display(value) {
  if (value === undefined || value === null || value === '') return '未知 / 未提供'
  if (Array.isArray(value)) return value.length ? [...value].sort().join('、') : '已保存列表为空'
  return String(value)
}
const comparison = computed(() => fields.map(([key,label]) => {
  const values = selected.value.map(row => display(row.value[key]))
  return {key,label,values,changed:values[0] !== values[1],unknown:values.every(value => value === '未知 / 未提供')}
}))
watch(() => props.context, () => { selected.value = [] })
</script>
<template>
  <section class="sample-comparison" aria-label="请求样本对照">
    <h3>请求样本对照 <small>{{ selected.length }}/2</small></h3>
    <p class="dim">从当前页选择两份样本，可跨样本分页保留选择。仅对照已保存摘要，不比较参数值、凭据原值或响应正文；时间缺失时不推断先后。名称列表可能截断。</p>
    <div class="sample-choices"><button v-for="row in rows" :key="row.id" type="button" :aria-pressed="selected.some(item => item.id === row.id)" :disabled="selected.length === 2 && !selected.some(item => item.id === row.id)" @click="toggle(row)">{{ selected.some(item => item.id === row.id) ? '取消样本' : '选择样本' }} #{{ row.id }} · {{ display(sample(row).status) }}</button></div>
    <div v-if="selected.length" class="selected-samples"><div v-for="row in selected" :key="row.id"><strong>样本 #{{ row.id }}</strong><small class="mono">{{ row.value.record_path || '来源位置未知' }}</small><p v-if="row.value.sample_truncated || row.value.truncated" class="warn">此样本摘要已截断，未列出的名称不能据此认定不存在。</p><button type="button" class="ghost" @click="selected = selected.filter(item => item.id !== row.id)">移除 #{{ row.id }}</button><button v-if="row.evidence_id" type="button" class="ghost" @click="emit('evidence', row.evidence_id)">打开样本 #{{ row.id }} 证据</button></div></div>
    <div v-if="selected.length === 2" class="comparison-scroll"><table><thead><tr><th>摘要字段</th><th v-for="row in selected" :key="row.id">样本 #{{ row.id }}</th><th>对照</th></tr></thead><tbody><tr v-for="field in comparison" :key="field.key" :class="{different:field.changed}"><th>{{ field.label }}</th><td v-for="(value,index) in field.values" :key="index">{{ value }}</td><td>{{ field.unknown ? '均未提供' : field.changed ? '摘要不同' : '摘要相同' }}</td></tr></tbody></table></div>
  </section>
</template>
<style scoped>
.sample-comparison { border:1px solid var(--line); border-radius:8px; padding:12px; margin-bottom:16px; }
.sample-comparison h3 { margin:0 0 8px; }
.sample-choices { display:flex; flex-wrap:wrap; gap:6px; }
.sample-choices [aria-pressed=true] { border-color:var(--accent); background:var(--tint-active); }
.selected-samples { display:grid; gap:10px; margin:12px 0; overflow-wrap:anywhere; }
.selected-samples small { display:block; }
.comparison-scroll { overflow:auto; }
table { width:100%; border-collapse:collapse; font-size:12px; table-layout:fixed; }
th,td { text-align:left; padding:8px 5px; border-bottom:1px solid var(--line); vertical-align:top; overflow-wrap:anywhere; }
.different { background:var(--tint-active); }
</style>
