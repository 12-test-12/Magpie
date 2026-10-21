<script setup>
import { onBeforeUnmount, ref, watch } from 'vue'
import { post } from '../api.js'
import { store } from '../store.js'
const emit = defineEmits(['staged'])
const mode = ref('text'), text = ref(''), name = ref('pasted.txt'), url = ref(''), error = ref(''), busy = ref(false)
let generation = 0
watch(() => store.project, () => { generation++; text.value = ''; error.value = ''; busy.value = false })
onBeforeUnmount(() => {generation++})
async function file(event) {
  const selected = event.target.files?.[0], current = generation
  if (!selected) return
  if (selected.size > 16*1024*1024) { error.value = '单文件最多 16 MiB'; return }
  try {
    const content = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(await selected.arrayBuffer())
    if (current !== generation) return
    text.value = content; name.value = selected.name; mode.value = /\.(m?js)$/i.test(selected.name) ? 'js' : 'text'; error.value = ''
  } catch { if (current === generation) error.value = '文件不是有效 UTF-8 文本，请先明确转换编码' }
}
async function stage() {
  const current = generation, project = store.project
  if (new TextEncoder().encode(text.value).length > 16*1024*1024) { error.value = '输入超过 16 MiB'; return }
  busy.value = true; error.value = ''
  try {
    const result = await post('/input-stage', { project, text:text.value, name:name.value,
      resource_url:mode.value === 'js' ? url.value : undefined })
    if (current !== generation || project !== store.project) return
    emit('staged', {...result, parser:mode.value === 'js' ? 'js-manifest' : ''})
    text.value = ''
  } catch (err) { if (current === generation) error.value = err.message }
  finally { if (current === generation) busy.value = false }
}
</script>
<template>
  <details class="local-input"><summary>粘贴文本 / 关联本地 JS 文件</summary>
    <p class="dim">暂存后再使用下方导入按钮。JS 必须填写资源绝对 URL；不执行脚本，不自动推断运行基址。</p>
    <label>输入类型<select v-model="mode" aria-label="本地输入类型"><option value="text">普通文本（自动仲裁）</option><option value="js">JavaScript（显式关联 URL）</option></select></label>
    <input type="file" aria-label="选择本地文本或 JS 文件" @change="file" />
    <label>原始文件名<input v-model="name" aria-label="粘贴文件名" required maxlength="200" /></label>
    <label v-if="mode === 'js'">JS 资源 URL<input v-model="url" aria-label="JS 资源绝对 URL" placeholder="完整资源地址（HTTP 或 HTTPS）" /></label>
    <textarea v-model="text" aria-label="粘贴导入内容" rows="5" placeholder="粘贴文本，或选择 UTF-8 文件…" />
    <p v-if="error" role="alert">{{ error }}</p><button type="button" :disabled="busy || !text || !store.project || mode === 'js' && !url.trim()" @click="stage">{{ busy ? '暂存中…' : '暂存到导入列表' }}</button>
  </details>
</template>
<style scoped>.local-input { margin:16px 0; padding:12px; border:1px solid var(--line); } label { display:block; margin:8px 0; } textarea { width:100%; box-sizing:border-box; }</style>
