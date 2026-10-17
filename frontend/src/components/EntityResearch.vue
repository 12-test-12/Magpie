<script setup>
import { onBeforeUnmount, ref, watch } from 'vue'
import { put } from '../api.js'
import { store } from '../store.js'
import { params, requestSlot } from '../workspace.js'
const props = defineProps({ entityId: {type:Number, required:true} })
const data = ref(null), tags = ref(''), tier = ref(''), loading = ref(false), saving = ref(false), error = ref(''), message = ref('')
const slot = requestSlot()
let generation = 0
function load() {
  generation++; slot.cancel(); saving.value = false; data.value = null; error.value = ''; message.value = ''; loading.value = true
  slot.run('/workspace/entities/' + props.entityId + '/research?' + params({project:store.project}),
    result => { data.value = result; tags.value = (result.tags || []).join(', '); tier.value = result.criticality ?? '' },
    text => {error.value = text}, () => {loading.value = false})
}
async function save() {
  const current = generation, project = store.project, id = props.entityId
  saving.value = true; error.value = ''; message.value = ''
  const values = [...new Set(tags.value.split(/[,，]/).map(t => t.trim()).filter(Boolean))]
  try {
    const result = await put('/workspace/entities/' + id + '/research?' + params({project}), {
      version:data.value.version, criticality:tier.value === '' ? null : Number(tier.value),
      add_tags:values.filter(t => !(data.value.tags || []).includes(t)), remove_tags:(data.value.tags || []).filter(t => !values.includes(t)),
    })
    if (current !== generation || project !== store.project) return
    data.value = result; tags.value = result.tags.join(', '); message.value = '标签与重要度已保存'; store.dataRevision++
  } catch(err) { if (current === generation && project === store.project) error.value = err.message }
  finally { if (current === generation) saving.value = false }
}
watch(() => [store.project, props.entityId], load, {immediate:true})
onBeforeUnmount(() => {generation++; slot.cancel()})
</script>
<template>
  <section class="research-metadata"><h3>人工标签与重要度</h3><p class="dim">Tier 1 为最高业务重要度；分级不代表漏洞严重度。</p><p v-if="loading" role="status">正在读取标记…</p><form v-else-if="data" @submit.prevent="save"><label>标签（逗号分隔）<input v-model="tags" aria-label="人工标签" placeholder="core-system, payment" /></label><label>资产重要度<select v-model="tier" aria-label="资产重要度"><option value="">未分级</option><option v-for="n in [1,2,3]" :key="n" :value="n">Tier {{ n }}</option></select></label><button :disabled="saving" type="submit">{{ saving ? '保存中…' : '保存标签与重要度' }}</button></form><div v-if="error" role="alert"><p>{{ error }}</p><button @click="load">重新载入标记</button></div><p v-if="message" role="status">{{ message }}</p></section>
</template>
<style scoped>
.research-metadata { border-top:1px solid var(--line); margin-top:18px; padding-top:8px; }
label { display:flex; flex-direction:column; gap:6px; margin:10px 0; }
</style>
