<script setup>
import { onBeforeUnmount, reactive, ref, watch } from 'vue'
import { post } from '../api.js'
import { store } from '../store.js'
import { params, requestSlot } from '../workspace.js'

const props = defineProps({ findingId: { type: Number, required: true }, section: { type: String, required: true } })
const emit = defineEmits(['select', 'evidence'])
const findingDetail = ref(null), retestPage = ref(null), loading = ref(false), error = ref('')
const cursor = ref(null), previous = ref([])
const slot = requestSlot(), retestSlot = requestSlot()
let generation = 0
const triageForm = reactive({ status: 'candidate', severity: 'medium', reason: '', duplicate_of_id: null })
const triageSubmitting = ref(false), triageMessage = ref('')
const retestForm = reactive({ status: 'inconclusive', notes: '', evidence_id: null })
const retestSubmitting = ref(false), retestMessage = ref('')
const conflict = ref(false), reloading = ref(false)

async function loadFinding() {
  loading.value = true; error.value = ''
  const project = store.project, id = props.findingId
  await slot.run('/workspace/findings/' + id + '?' + params({ project }), result => {
    findingDetail.value = result
    Object.assign(triageForm, { status: result.triage.status, severity: result.triage.severity,
      reason: result.triage.disposition_reason || '', duplicate_of_id: result.triage.duplicate_of_id })
  }, message => { error.value = message }, () => { loading.value = false })
}
async function loadRetests() {
  loading.value = true; error.value = ''
  await retestSlot.run('/workspace/entities/' + props.findingId + '?' + params({
    project: store.project, section: 'retests', cursor: cursor.value, limit: 20,
  }), result => { retestPage.value = result }, message => { error.value = message }, () => { loading.value = false })
}
async function reload() {
  const current = generation
  await loadFinding()
  if (current === generation && props.section === 'retests' && findingDetail.value && !error.value) await loadRetests()
}
async function reloadConflict() {
  const current = generation
  reloading.value = true
  await reload()
  if (current !== generation) return
  reloading.value = false
  if (!error.value) {
    conflict.value = false
    triageMessage.value = ''; retestMessage.value = ''
  }
}
function turn(forward) {
  if (forward) { previous.value.push(cursor.value); cursor.value = retestPage.value.next_cursor }
  else cursor.value = previous.value.pop()
  loadRetests()
}
async function submitTriage() {
  if (conflict.value || !findingDetail.value || triageSubmitting.value || retestSubmitting.value) return
  if (triageForm.status !== 'candidate' && !triageForm.reason.trim()) { triageMessage.value = '该判定必须填写理由；没有证据时请明确说明缺口'; return }
  const current = generation, project = store.project, id = props.findingId
  triageSubmitting.value = true; triageMessage.value = ''
  try {
    await post('/workspace/findings/' + id + '/triage?' + params({ project }), {
      expected_version: findingDetail.value.triage.version,
      status: triageForm.status, severity: triageForm.severity,
      reason: triageForm.reason.trim() || undefined,
      duplicate_of_id: triageForm.status === 'duplicate' && triageForm.duplicate_of_id ? Number(triageForm.duplicate_of_id) : undefined,
    })
    if (current !== generation) return
    triageMessage.value = '处置判定已更新并计入审计'
    await loadFinding()
    if (current === generation) store.dataRevision++
  } catch (err) { if (current === generation) { triageMessage.value = err.message; conflict.value = err.code === 'E_STALE_FINDING' } }
  finally { if (current === generation) triageSubmitting.value = false }
}
async function submitRetest() {
  if (conflict.value || !findingDetail.value || triageSubmitting.value || retestSubmitting.value) return
  if (!retestForm.notes.trim()) { retestMessage.value = '复测记录说明不能为空'; return }
  const current = generation, project = store.project, id = props.findingId
  retestSubmitting.value = true; retestMessage.value = ''
  try {
    await post('/workspace/findings/' + id + '/retest?' + params({ project }), {
      expected_version: findingDetail.value.triage.version,
      status: retestForm.status, notes: retestForm.notes.trim(),
      evidence_id: retestForm.evidence_id ? Number(retestForm.evidence_id) : undefined,
    })
    if (current !== generation) return
    retestMessage.value = retestForm.status === 'inconclusive' ? '复测记录已添加；原处置判定保留' : '复测记录已添加并联动处置状态'
    retestForm.notes = ''; retestForm.evidence_id = null
    cursor.value = null; previous.value = []
    await reload()
    if (current === generation) store.dataRevision++
  } catch (err) { if (current === generation) { retestMessage.value = err.message; conflict.value = err.code === 'E_STALE_FINDING' } }
  finally { if (current === generation) retestSubmitting.value = false }
}
watch(() => [store.project, props.findingId, props.section], () => {
  generation++; slot.cancel(); retestSlot.cancel()
  findingDetail.value = null; retestPage.value = null; cursor.value = null; previous.value = []
  triageMessage.value = ''; retestMessage.value = ''; triageSubmitting.value = false; retestSubmitting.value = false
  conflict.value = false; reloading.value = false
  retestForm.notes = ''; retestForm.evidence_id = null
  reload()
}, { immediate: true })
onBeforeUnmount(() => { generation++; slot.cancel(); retestSlot.cancel() })
</script>

<template>
  <div class="finding-panel">
    <div v-if="conflict" role="alert" class="error-state"><p>发现判定或复测已被修改，本次未写入。草稿暂保留，请复制需要的内容，再重新载入最新记录并检查后保存。</p><button :disabled="reloading" @click="reloadConflict">重新载入最新记录（替换判定表单）</button></div>
    <p v-if="loading" role="status">正在载入发现…</p>
    <div v-if="error" role="alert" class="error-state"><p>{{ error }}</p><button @click="reload">重试</button></div>
      <section v-if="section === 'triage' && findingDetail" class="triage-section">
        <div class="triage-box">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
            <strong>处置研判状态</strong>
            <span :class="['badge-status', 'status-' + (findingDetail?.triage?.status || 'candidate')]">
              {{ findingDetail?.triage?.status || 'candidate' }}
            </span>
          </div>
          <p v-if="findingDetail?.title"><strong>漏洞标题</strong>：{{ findingDetail.title }}</p>
          <p v-if="findingDetail?.target_asset">
            <strong>影响目标</strong>：
            <button class="ghost inline-link" @click="emit('select', { id: findingDetail.target_asset.id, kind: findingDetail.target_asset.kind })">
              {{ findingDetail.target_asset.display_key }} ↗
            </button>
          </p>
          <p v-if="findingDetail?.triage?.disposition_reason"><strong>研判依据</strong>：{{ findingDetail.triage.disposition_reason }}</p>
          <p v-if="findingDetail?.decision_basis_missing" class="notice">依据缺口：历史记录未填写判定理由，尚需人工核查。</p>
          <p v-if="findingDetail?.triage?.duplicate_of_id">
            <strong>主漏洞</strong>：
            <button class="ghost inline-link" @click="emit('select', { id: findingDetail.triage.duplicate_of_id, kind: 'vuln' })">
              #{{ findingDetail.triage.duplicate_of_id }} {{ findingDetail.triage.duplicate_of_display || '' }} ↗
            </button>
          </p>
          <small v-if="findingDetail?.triage?.updated_at" class="dim">最后更新：{{ findingDetail.triage.updated_at }} · {{ findingDetail.triage.updated_by || 'analyst' }}</small>
        </div>

        <form class="modal-form" @submit.prevent="submitTriage" style="background:#19222a; padding:12px; border-radius:8px; border:1px solid #2b3941;">
          <h3 style="margin:0 0 8px; font-size:12px;">更新处置决策</h3>
          <label>处置状态
            <select v-model="triageForm.status">
              <option value="confirmed">已确认 (confirmed)</option>
              <option value="candidate">待核实 (candidate)</option>
              <option value="resolved">已修复 (resolved)</option>
              <option value="false_positive">误报排除 (false_positive)</option>
              <option value="duplicate">重复发现 (duplicate)</option>
              <option value="inconclusive">存疑 (inconclusive)</option>
            </select>
          </label>
          <label>严重级别
            <select v-model="triageForm.severity">
              <option value="critical">严重 (critical)</option>
              <option value="high">高危 (high)</option>
              <option value="medium">中危 (medium)</option>
              <option value="low">低危 (low)</option>
              <option value="info">提示 (info)</option>
            </select>
          </label>
          <label v-if="triageForm.status === 'duplicate'">主漏洞 ID
            <input v-model="triageForm.duplicate_of_id" type="number" placeholder="主漏洞实体 ID" required />
          </label>
          <label>判定理由 / 核实说明
            <textarea v-model="triageForm.reason" :required="triageForm.status !== 'candidate'" placeholder="记录处置决策依据；没有证据时明确说明缺口…" rows="3" />
          </label>
          <button type="submit" class="primary" :disabled="triageSubmitting || retestSubmitting || conflict || loading || !!error" style="margin-top:6px;">
            {{ triageSubmitting ? '保存中…' : '更新处置状态' }}
          </button>
          <p v-if="triageMessage" :class="{ 'error-state': triageMessage.includes('失败') || triageMessage.includes('error'), dim: !triageMessage.includes('失败') }" style="margin:6px 0 0; font-size:11px;">
            {{ triageMessage }}
          </p>
        </form>
      </section>

      <section v-else-if="section === 'retests' && findingDetail" class="retest-section">
        <div class="retest-warn">
          ⚠️ 网络超时、无法连接或登录失效严禁判定为已修复（resolved）。复测必须基于明确的重现与修复验证证据。
        </div>

        <div v-if="retestPage?.rows?.length" class="retests-history">
          <div v-for="rt in retestPage.rows" :key="rt.id" class="retest-item">
            <div class="retest-item-header">
              <span :class="['badge-status', 'status-' + rt.status]">{{ rt.status }}</span>
              <span class="dim">{{ rt.created_at }} · {{ rt.actor }}</span>
            </div>
            <p style="margin:4px 0; font-size:11px; white-space:pre-wrap;">{{ rt.notes }}</p>
            <div v-if="rt.evidence_id">
              <button class="ghost inline-link" @click="emit('evidence', rt.evidence_id)">查看复测关联证据 #{{ rt.evidence_id }} →</button>
            </div>
            <p v-else class="notice">未关联复测证据，仅有人工说明。</p>
          </div>
        </div>
        <p v-else class="dim" style="margin-bottom:14px;">暂无人工复测记录。</p>

        <div v-if="retestPage" class="detail-pagination">
          <span>{{ retestPage.total }} 次复测 · 本页 {{ retestPage.rows.length }} 条</span>
          <button :disabled="!previous.length || loading" @click="turn(false)">上一页</button>
          <button :disabled="!retestPage.next_cursor || loading" @click="turn(true)">下一页</button>
        </div>
        <form class="modal-form" @submit.prevent="submitRetest" style="background:#19222a; padding:12px; border-radius:8px; border:1px solid #2b3941;">
          <h3 style="margin:0 0 8px; font-size:12px;">＋ 添加人工复测记录</h3>
          <label>复测结论 *
            <select v-model="retestForm.status">
              <option value="still_vulnerable">漏洞仍存在 (still_vulnerable)</option>
              <option value="resolved">已修复验证 (resolved)</option>
              <option value="inconclusive">存疑/无法下结论 (inconclusive)</option>
            </select>
          </label>
          <label>复测说明与现象 *
            <textarea v-model="retestForm.notes" placeholder="记录复测操作、响应状态及验证依据…" rows="3" required />
          </label>
          <label>关联复测证据 ID
            <input v-model="retestForm.evidence_id" type="number" placeholder="证据 ID (可选)" />
            <small v-if="!retestForm.evidence_id" class="dim">未关联复测证据，仅有人工说明；系统不会据此验证修复真实性。</small>
          </label>
          <button type="submit" class="primary" :disabled="retestSubmitting || triageSubmitting || conflict || loading || !!error" style="margin-top:6px;">
            {{ retestSubmitting ? '保存中…' : '提交复测记录' }}
          </button>
          <p v-if="retestMessage" :class="{ 'error-state': retestMessage.includes('失败') || retestMessage.includes('空'), dim: !retestMessage.includes('失败') }" style="margin:6px 0 0; font-size:11px;">
            {{ retestMessage }}
          </p>
        </form>
      </section>

  </div>
</template>
