<script setup>
import { ref, watch } from 'vue'
import { get, post } from '../api.js'
import { store } from '../store.js'
import ImportCoverage from './ImportCoverage.vue'

const props = defineProps({ path: { type: String, default: '' } })
const emit = defineEmits(['ready'])
const preview = ref(null)
const mapping = ref({})
const profiles = ref([])
const selected = ref('')
const profileName = ref('')
const error = ref('')
const message = ref('')
const busy = ref(false)
const confirmed = ref(false)
let revision = 0
const kinds = { auto: '自动判断', url: '完整 URL', domain: '域名 / 主机', ip: 'IP',
  port: '端口', status: 'HTTP 状态', method: 'HTTP 方法', technology: '技术信息',
  ignore: '人工忽略', unknown: '保留未识别' }

function invalidate() { confirmed.value = false; emit('ready', null) }
watch(() => [props.path, store.project], () => {
  revision++; preview.value = null; mapping.value = {}; profiles.value = []
  busy.value = false; error.value = ''; message.value = ''; invalidate()
})
watch(mapping, invalidate, { deep: true, flush: 'sync' })

async function inspect() {
  invalidate()
  const current = ++revision
  busy.value = true; error.value = ''; message.value = ''
  try {
    const data = await post('/import-preview', { path: props.path,
      project: store.project || null, mapping: mapping.value })
    if (current !== revision) return
    preview.value = data
    const saved = await get('/import-profiles?project=' + encodeURIComponent(store.project || ''))
    if (current === revision) profiles.value = saved
  } catch (err) { if (current === revision) error.value = err.message }
  finally { if (current === revision) busy.value = false }
}

function applyProfile() {
  const profile = profiles.value.find(p => p.id === selected.value)
  if (!profile) return
  if (profile.structure_signature !== preview.value?.structure_signature) {
    error.value = '结构与配置不匹配，请手动重新映射。'; return
  }
  mapping.value = { ...profile.mapping }; message.value = '配置已应用，请重新预览。'
}

async function saveProfile() {
  error.value = ''; message.value = ''
  try {
    await post('/import-profiles', { project: store.project || null, name: profileName.value,
      structure_signature: preview.value.structure_signature, mapping: mapping.value })
    message.value = '映射配置已保存到当前项目；不会保存数据样例。'
  } catch (err) { error.value = err.message }
}

function confirm() {
  if (!preview.value?.complete || preview.value.error ||
      JSON.stringify(preview.value.mapping) !== JSON.stringify(mapping.value)) {
    error.value = '映射已修改或预览未完成，请重新预览后确认。'; confirmed.value = false; return
  }
  confirmed.value = !confirmed.value
  emit('ready', confirmed.value ? { mapping: { ...mapping.value },
    sha256: preview.value.sha256, path: props.path } : null)
}
</script>

<template>
  <section class="universal" aria-label="通用字段映射">
    <h2>陌生工具 · 通用字段映射</h2>
    <p class="dim">选一个文件进行只读预览（最大 16 MiB）。支持 JSON、JSONL、CSV、TSV、XML 和逐行文本。不会向目标发出请求。</p>
    <button type="button" :disabled="busy || !path" @click="inspect">{{ busy ? '分析中…' : '预览格式与覆盖' }}</button>
    <p v-if="error" role="alert" class="bad">{{ error }}</p>
    <p v-if="message" role="status">{{ message }}</p>
    <template v-if="preview">
      <ImportCoverage :coverage="preview" />
      <div class="profile-row" v-if="profiles.length">
        <select v-model="selected" aria-label="已有映射配置"><option value="">选择已有配置</option>
          <option v-for="p in profiles" :key="p.id" :value="p.id">{{ p.name }}</option></select>
        <button type="button" @click="applyProfile">应用配置</button>
      </div>
      <div class="mapping-fields">
        <div v-for="(field, key) in preview.fields" :key="key" class="mapping-row">
          <code>{{ key }}</code><span :title="field.sample">{{ field.sample }}</span>
          <select :value="mapping[key] || 'auto'" :aria-label="`映射 ${key}`"
                  @change="mapping = { ...mapping, [key]: $event.target.value }">
            <option v-for="(label, value) in kinds" :key="value" :value="value">{{ label }}</option>
          </select>
        </div>
      </div>
      <p class="dim">修改后重新预览。未映射字段保留为待处理信息；忽略必须由你明确选择。</p>
      <div class="profile-row">
        <input v-model="profileName" aria-label="映射配置名称" placeholder="配置名称，例如 MyScanner CSV" maxlength="80" />
        <button type="button" :disabled="!profileName || !preview.complete" @click="saveProfile">保存映射</button>
      </div>
      <button type="button" :disabled="busy || !preview.complete || !!preview.error" @click="confirm">
        {{ confirmed ? '已确认映射（点击取消）' : '确认此映射，用于导入' }}
      </button>
    </template>
  </section>
</template>

<style scoped>
.universal { margin: 14px 0; padding: 14px; border: 1px solid var(--accent); border-radius: 8px; font-size: 12px; }
h2 { font-size: 14px; margin: 0 0 8px; }
.mapping-fields { max-height: 260px; overflow: auto; }
.mapping-row { display: grid; grid-template-columns: 1.2fr 1fr 150px; gap: 10px; align-items: center; padding: 6px 0; }
.mapping-row code, .mapping-row span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.profile-row { display: flex; gap: 8px; margin: 12px 0; }
select, input { min-width: 0; width: 100%; }
button { flex-shrink: 0; }
</style>
