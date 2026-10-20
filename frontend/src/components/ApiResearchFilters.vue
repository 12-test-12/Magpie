<script setup>
import { computed, ref } from 'vue'
const props = defineProps({ modelValue: { type: Object, required: true }, applied: { type: Object, required: true } })
const emit = defineEmits(['update:modelValue'])
const expanded = ref(false)
const appliedLabels = computed(() => {
  const labels = {parameter_name:'参数名',parameter_location:'位置',response_status:'状态码',response_mime:'MIME',credential:'凭据',api_origin:'来源'}
  const values = {http_sample:'有请求样本',js_only:'JS 静态来源、无样本',declaration_only:'仅契约声明',cookie:'Cookie',bearer:'Bearer',basic:'Basic',api_key:'API key',other:'其他凭据',unknown:'未知',no_recognized_credentials_observed:'未观察到已识别凭据'}
  return Object.entries(labels).filter(([key]) => props.applied[key] !== null && props.applied[key] !== undefined && props.applied[key] !== '').map(([key,label]) => `${label}：${values[props.applied[key]] || props.applied[key]}`)
})
defineExpose({ collapse: () => { expanded.value = false } })
function update(model, key, value) { emit('update:modelValue', { ...model, [key]: value }) }
</script>
<template>
  <details class="api-research-filters" :open="expanded" @toggle="expanded = $event.target.open">
    <summary>API 研究条件 <small>{{ appliedLabels.length ? `已应用 ${appliedLabels.length} 项` : '展开设置参数、响应、凭据与来源' }}</small><span v-if="appliedLabels.length" class="applied-filters">{{ appliedLabels.join(' · ') }}</span></summary>
    <div class="research-filter-grid">
      <label>参数名（精确）<input :value="modelValue.parameter_name" aria-label="参数名筛选" maxlength="120" placeholder="例如 redirect / user_id" @input="update(modelValue, 'parameter_name', $event.target.value)" /></label>
      <label>参数位置<select :value="modelValue.parameter_location" aria-label="参数位置筛选" @change="update(modelValue, 'parameter_location', $event.target.value)"><option value="">全部位置</option><option v-for="location in ['query','body','path','header','cookie','unknown']" :key="location">{{ location }}</option></select></label>
      <label>响应状态码<input :value="modelValue.response_status" type="number" min="100" max="599" aria-label="响应状态码筛选" placeholder="例如 200" @input="update(modelValue, 'response_status', $event.target.value === '' ? null : Number($event.target.value))" /></label>
      <label>响应 MIME<input :value="modelValue.response_mime" aria-label="响应类型筛选" maxlength="120" placeholder="例如 application/json" @input="update(modelValue, 'response_mime', $event.target.value)" /></label>
      <label>凭据线索<select :value="modelValue.credential" aria-label="凭据线索筛选" @change="update(modelValue, 'credential', $event.target.value)"><option value="">全部凭据线索</option><option value="cookie">观察到 Cookie</option><option value="bearer">观察到 Bearer</option><option value="basic">观察到 Basic</option><option value="api_key">观察到 API key</option><option value="other">观察到其他凭据</option><option value="unknown">凭据状态未知</option><option value="no_recognized_credentials_observed">未观察到已识别凭据</option></select></label>
      <label>来源性质<select :value="modelValue.api_origin" aria-label="来源性质筛选" @change="update(modelValue, 'api_origin', $event.target.value)"><option value="">全部来源性质</option><option value="http_sample">有 HTTP 请求样本</option><option value="js_only">有 JS 静态来源、无请求样本</option><option value="declaration_only">仅契约声明、无请求样本或 JS 来源</option></select></label>
    </div>
    <p class="dim">响应、凭据与参数组合须在同一份请求样本中成立；样本中的参数只支持 query/body/header 名称。参数名区分大小写，% 和 _ 按字面匹配。未命中不代表不存在，缺少凭据不代表无鉴权。</p>
  </details>
</template>
<style scoped>
.api-research-filters { flex-basis:100%; min-width:0; border:1px solid var(--line); border-radius:8px; padding:12px; }
.api-research-filters summary { cursor:pointer; font-weight:600; overflow-wrap:anywhere; }
.api-research-filters summary small { font-weight:normal; margin-left:8px; color:var(--dim); }
.applied-filters { display:block; font-weight:normal; font-size:12px; color:var(--dim); margin-top:6px; }
.api-research-filters[open] summary { margin-bottom:12px; }
.research-filter-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; }
.research-filter-grid label { min-width:0; display:flex; flex-direction:column; gap:5px; }
.research-filter-grid input,.research-filter-grid select { width:100%; min-width:0; box-sizing:border-box; }
.api-research-filters p { margin:10px 0 0; line-height:1.6; }
@media (max-width:1100px) { .research-filter-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } }
</style>
