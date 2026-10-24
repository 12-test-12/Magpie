<script setup>
import { computed, reactive, watch } from 'vue'
const props = defineProps({ fields: { type: Array, required: true }, version: { type: Number, required: true } })
const emit = defineEmits(['apply', 'cancel'])
const choices = reactive({})
watch(() => props.fields, fields => {
  for (const key of Object.keys(choices)) delete choices[key]
  for (const field of fields) choices[field.key] = field.choice
}, { immediate: true })
const ready = computed(() => props.fields.every(field => ['latest', 'draft'].includes(choices[field.key])))
function apply() {
  if (ready.value) emit('apply', Object.fromEntries(props.fields.map(field => [field.key, field[choices[field.key]]])))
}
</script>
<template>
  <section class="conflict-resolver" aria-label="核对冲突字段">
    <h4>逐项核对 · 最新版本 {{ version }}</h4>
    <p>未改动的字段默认保留最新值。双方修改不同的字段必须逐项选择；应用选择只更新草稿，随后仍需手动保存。</p>
    <article v-for="field in fields" :key="field.key">
      <strong>{{ field.label }}{{ field.conflict ? ' · 双方修改不同，请选择' : '' }}</strong>
      <div class="merge-values"><div v-for="(label,key) in {original:'原读取值',latest:'最新值',draft:'我的草稿'}" :key="key"><small>{{ label }}</small><pre>{{ field.display[key] }}</pre></div></div>
      <label>{{ field.label }}采用
        <select v-model="choices[field.key]" :aria-label="'字段选择：' + field.label">
          <option disabled value="">请选择要保留的值</option><option value="latest">采用最新值</option><option value="draft">保留我的修改</option>
        </select>
      </label>
    </article>
    <div class="merge-actions"><button type="button" :disabled="!ready" @click="apply">应用选择到草稿</button><button type="button" class="ghost" @click="emit('cancel')">取消核对，保留草稿</button></div>
  </section>
</template>
<style scoped>
.conflict-resolver { border:1px solid var(--line); padding:12px; margin:12px 0; min-width:0; }
article { border-top:1px solid var(--line); padding:12px 0; }
.merge-values { display:grid; grid-template-columns:repeat(auto-fit,minmax(min(170px,100%),1fr)); gap:8px; margin:8px 0; }
.merge-values > div { min-width:0; }
pre { white-space:pre-wrap; overflow-wrap:anywhere; max-height:140px; overflow:auto; margin:4px 0; }
select { max-width:100%; }
.merge-actions { display:flex; flex-wrap:wrap; gap:8px; }
</style>
