<script setup>
import { onBeforeUnmount, reactive, ref, watch } from 'vue'
import { store } from '../store.js'
import { API_PREFIX, get, post, token } from '../api.js'
import { params } from '../workspace.js'
const emit = defineEmits(['created'])
let generation = 0
const showCreateModal = ref(false)
const createForm = reactive({ title: '', target_id: '', severity: 'high', status: 'candidate', reason: '', description: '' })
const createSubmitting = ref(false)
const createError = ref('')

async function submitCreateFinding() {
  const current = generation, project = store.project
  createError.value = ''
  if (!createForm.title.trim()) { createError.value = '漏洞标题不能为空'; return }
  if (!createForm.target_id) { createError.value = '目标实体 ID 不能为空'; return }
  if (createForm.status !== 'candidate' && !createForm.reason.trim()) { createError.value = '已确认发现必须填写判定理由；没有证据时请明确说明缺口'; return }
  createSubmitting.value = true
  try {
    await post('/workspace/findings?' + params({ project }), {
      title: createForm.title.trim(),
      target_id: Number(createForm.target_id),
      severity: createForm.severity,
      status: createForm.status,
      reason: createForm.reason.trim() || undefined,
      description: createForm.description.trim() || undefined,
    })
    if (current !== generation) return
    showCreateModal.value = false
    Object.assign(createForm, { title: '', target_id: '', severity: 'high', status: 'candidate', reason: '', description: '' })
    emit('created')
  } catch (err) {
    if (current === generation) createError.value = err.message
  } finally {
    if (current === generation) createSubmitting.value = false
  }
}

const showExportModal = ref(false)
const exporting = ref(false)
const exportError = ref('')

async function downloadReport(format) {
  const current = generation, project = store.project
  exportError.value = ''
  exporting.value = true
  try {
    if (format === 'json') {
      const reportData = await get('/workspace/report?' + params({ project, format: 'json' }))
      if (current !== generation) return
      const blob = new Blob([JSON.stringify(reportData, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `magpie-report-${project}.json`
      a.click()
      URL.revokeObjectURL(url)
    } else {
      const res = await fetch(`${API_PREFIX}/workspace/report?` + params({ project, format: 'markdown' }), {
        headers: { Authorization: `Bearer ${token.value}` },
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const text = await res.text()
      if (current !== generation) return
      const blob = new Blob([text], { type: 'text/markdown; charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `magpie-report-${project}.md`
      a.click()
      URL.revokeObjectURL(url)
    }
  } catch (err) {
    if (current === generation) exportError.value = err.message
  } finally {
    if (current === generation) exporting.value = false
  }
}

watch(() => store.project, () => {
  generation++; showCreateModal.value = false; showExportModal.value = false
  createError.value = ''; exportError.value = ''; createSubmitting.value = false; exporting.value = false
  Object.assign(createForm, { title: '', target_id: '', severity: 'high', status: 'candidate', reason: '', description: '' })
})
onBeforeUnmount(() => { generation++ })
</script>
<template>
  <div class="finding-actions" style="display:contents">
    <button class="ghost" @click="showCreateModal = true">＋ 人工建发现</button>
    <button class="ghost" @click="showExportModal = true">📄 导出报告</button>
    <!-- 创建人工漏洞发现弹窗 -->
    <div v-if="showCreateModal" class="modal-backdrop" @click.self="showCreateModal = false">
      <div class="modal-dialog">
        <header><h3>＋ 人工录入漏洞发现</h3><button class="ghost" @click="showCreateModal = false">✕</button></header>
        <form class="modal-form" @submit.prevent="submitCreateFinding">
          <label>漏洞标题 *<input v-model="createForm.title" placeholder="例如：管理后台未授权访问" maxlength="300" required /></label>
          <label>目标实体 ID *<input v-model="createForm.target_id" type="number" placeholder="关联主机或接口实体 ID" required /><small class="dim">必须是当前项目已存在的实体 ID（如 host/endpoint）</small></label>
          <label>严重级别<select v-model="createForm.severity"><option value="critical">严重 (critical)</option><option value="high">高危 (high)</option><option value="medium">中危 (medium)</option><option value="low">低危 (low)</option><option value="info">提示 (info)</option></select></label>
          <label>初始处置状态<select v-model="createForm.status"><option value="confirmed">已确认 (confirmed)</option><option value="candidate">待核实 (candidate)</option></select></label>
          <label>判定理由 / 核实依据<textarea v-model="createForm.reason" :required="createForm.status !== 'candidate'" placeholder="说明核实依据；缺少证据时请明确记录缺口…" /></label>
          <label>详细描述与复现细节<textarea v-model="createForm.description" placeholder="漏洞现象、影响接口与复现步骤…" /></label>
          <p v-if="createError" class="error-state" style="margin: 0; font-size: 11px;">{{ createError }}</p>
          <div class="modal-actions">
            <button type="button" class="ghost" @click="showCreateModal = false">取消</button>
            <button type="submit" class="primary" :disabled="createSubmitting">{{ createSubmitting ? '保存中…' : '创建漏洞' }}</button>
          </div>
        </form>
      </div>
    </div>

    <!-- 导出报告弹窗 -->
    <div v-if="showExportModal" class="modal-backdrop" @click.self="showExportModal = false">
      <div class="modal-dialog">
        <header><h3>📄 导出安全研究与漏洞发现报告</h3><button class="ghost" @click="showExportModal = false">✕</button></header>
        <p class="dim" style="font-size: 11px; line-height: 1.6; margin-bottom: 16px;">报告基于本地已导入数据、人工处置判定与复测事实生成，严格恪守单一事实源与脱敏审计纪律。</p>
        <div style="display: flex; gap: 12px; margin-bottom: 20px;">
          <button class="primary" :disabled="exporting" @click="downloadReport('markdown')">下载 Markdown 报告 (.md)</button>
          <button class="secondary" :disabled="exporting" @click="downloadReport('json')">下载结构化 JSON (.json)</button>
        </div>
        <p v-if="exportError" class="error-state" style="margin: 0; font-size: 11px;">{{ exportError }}</p>
        <div class="modal-actions">
          <button type="button" class="ghost" @click="showExportModal = false">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>
