<script setup>
/**
 * 导入对话框 —— 顶部那个「导入」按钮，也就是 n8n 外壳里的 **Run**（§7.1）。
 *
 * 它走 2.7 的两条路，一条都不绕：
 *
 *   `POST /imports`  → 立刻返回 `imp-N`（202），**不等它跑完**
 *   `GET /imports/{id}/events`（SSE）→ 进度 / 阶段 / 日志 / 坏行，每 100ms 一批
 *
 * 三件刻意做的事：
 *
 *   1. **进度只显示数据层给的数**（`ingest` 的 `on_progress`）。界面**不自己
 *      数数** —— 那样迟早会出现"进度条说 80%，报告说 100%"，而两者都自称是
 *      权威。2.7 的注释把这条写死了，这里照办。
 *   2. **路径要绝对路径**。服务进程按**它自己的**工作目录解析相对路径，
 *      而这个目录不是用户以为的那个。`POST /imports` 会在提交之前就检查路径
 *      存不存在并 400 —— 那比让用户等一个"没有匹配到任何文件"的作业好得多。
 *   3. **丢帧要说出来**。SSE 的背压策略允许丢帧（慢消费者不阻塞导入），但
 *      `dropped` 计数会原样显示。
 *
 * 拿到路径的**三条路**（0.17.0 起前两条是主流入口）：
 *
 *   * **原生选择器 / 拖拽**：浏览器给的是文件**内容**而不是路径（安全模型
 *     如此），所以内容先走 `POST /uploads` 落进服务端暂存区，再把服务进程
 *     自己写的绝对路径填回来 —— 选中的与导入读到的出自同一个进程。
 *   * **服务端目录点选**（`GET /fs`）：不传内容、直接要路径，适合大文件。
 *   * **手填**：路径按服务进程的工作目录解析，写错在提交前就被拒。
 *
 * 上传只是**拿路径**：表单不自动提交，parser / dry-run 这些选项照旧由用户
 * 看一眼再点「开始导入」。
 */
import { computed, ref } from 'vue'
import { upload, watchImport } from '../api.js'
import { store } from '../store.js'
import FsPicker from '../components/FsPicker.vue'

const emit = defineEmits(['close'])

const paths = ref('')
const parser = ref('')
const archive = ref('archived')
const dryRun = ref(false)
const busy = ref(false)
const error = ref('')
const phases = ref([])
const picker = ref(false)
const fileInput = ref(null)
const dragOver = ref(false)
//: > 0 表示正在上传的文件数（0 = 空闲）。与提交的 `busy` 是两件事：
//: 上传完路径才进表单，"上传中"点提交会导一个不完整的批次。
const uploading = ref(0)

const pathList = computed(() =>
  paths.value.split('\n').map((line) => line.trim()).filter(Boolean))

/**
 * 从浏览框/上传选中一个路径：**追加**，不覆盖 —— 手填的与点选的可以混着来。
 * 已经在表单里的不加第二行（同一个文件导两遍是纯粹的坏行来源）。
 */
function addPath(picked) {
  if (!pathList.value.includes(picked)) {
    paths.value = [...pathList.value, picked].join('\n')
  }
  picker.value = false
}

/**
 * 原生选择器 / 拖拽进来的文件 → 上传暂存区 → 路径进表单。
 * 一次请求带一批（FormData 多个 `files` 字段），后端一批一个子目录。
 */
async function handleFiles(list) {
  const files = Array.from(list || [])
  dragOver.value = false
  if (!files.length) return
  uploading.value = files.length
  error.value = ''
  try {
    const form = new FormData()
    for (const file of files) form.append('files', file, file.name)
    const data = await upload('/uploads', form)
    for (const item of data.files || []) addPath(item.path)
    append(`已上传 ${data.count} 个文件到暂存区 —— 路径已填入下方，` +
      '确认解析器选项后点「开始导入」。')
  } catch (err) {
    error.value = `上传失败：${err.message}`
  } finally {
    uploading.value = 0
  }
}

function onDrop(event) {
  handleFiles(event.dataTransfer && event.dataTransfer.files)
}

function onPick(event) {
  handleFiles(event.target.files)
  event.target.value = ''                 // 同一个文件再选一次也要触发 change
}

function append(text, level = 'info') {
  phases.value = phases.value.concat([{ text, level }])
  store.pushLog(text, 'import', level)
}

async function submit() {
  if (!pathList.value.length) {
    error.value = '至少填一个文件或目录。'
    return
  }
  busy.value = true
  error.value = ''
  phases.value = []
  try {
    const record = await store.submitImport({
      paths: pathList.value, parser: parser.value.trim(),
      dryRun: dryRun.value, archive: archive.value,
    })
    append(`已提交 ${record.import_id}（${pathList.value.length} 个路径）—— ` +
      '作业已排队，进度从这里开始实时推送。')
    store.drawerOpen = true
    watchImport(record.import_id, onBatch, () => append('事件流已连上。'),
      () => append('事件流断开。', 'warn'))
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = false
  }
}

function onBatch(payload) {
  if (payload.dropped) {
    store.importProgress.dropped += payload.dropped
    append(`（队列满，丢了 ${payload.dropped} 条事件 —— 丢帧不静默，但确实丢了）`,
      'warn')
  }
  for (const event of payload.events || []) {
    const data = event.data || {}
    if (event.type === 'progress') {
      // 进度**只**来自数据层的计数，界面一个数都不自己加
      store.importProgress.records = data.records ?? store.importProgress.records
      store.importProgress.line = data.line ?? store.importProgress.line
      store.importProgress.badLines =
        (data.counts && data.counts.bad_lines) ?? store.importProgress.badLines
      store.importProgress.stage = data.stage || store.importProgress.stage
      append(`进度 ${data.file || ''}：${data.records} 条记录 / ${data.line} 行` +
        `，坏行 ${data.counts ? data.counts.bad_lines : 0}`)
    } else if (event.type === 'job_done') {
      append(`作业 ${data.job_id} 结束：${data.state} ${JSON.stringify(data.counts || {})}`,
        data.state === 'done' ? 'info' : 'warn')
    } else if (event.type === 'done') {
      store.importProgress.state = data.state
      append(`导入结束：${data.state}，job ${JSON.stringify(data.job_ids || [])}`,
        data.state === 'done' ? 'info' : 'warn')
      store.reload()
      store.loadMeta()
    } else if (event.type === 'error') {
      store.importProgress.state = 'failed'
      append(`出错：${data.error}`, 'error')
    } else {
      store.importProgress.stage = store.importProgress.stage || event.type
      append(`${event.type} ${JSON.stringify(data).slice(0, 160)}`)
    }
  }
}
</script>

<template>
  <div class="scrim" @click.self="emit('close')">
    <div class="dialog panel">
      <h1>导入</h1>
      <p class="dim small">
        把 subfinder / httpx 的输出收进来。首选：点「选择文件…」弹系统对话框
        （可多选）或直接把文件拖进来 —— 文件会先传到服务端暂存区换回绝对路径。
        大文件可用「服务端目录…」在目录树里点选（不传内容），或直接手填 ——
        手填时**路径按服务进程的工作目录解析**，写错会在提交之前就被拒。
      </p>

      <input ref="fileInput" type="file" multiple hidden
             @change="onPick" />
      <div class="drop" :class="{ over: dragOver }"
           @dragover.prevent="dragOver = true"
           @dragleave="dragOver = false"
           @drop.prevent="onDrop">
        <button type="button" class="ghost" :disabled="uploading > 0"
                @click="fileInput && fileInput.click()">
          {{ uploading > 0 ? `正在上传 ${uploading} 个文件…` : '选择文件…' }}
        </button>
        <span class="dim small">或把文件拖到这里</span>
      </div>

      <div class="labelrow">
        <label>文件或目录（每行一个）</label>
        <button type="button" class="ghost" @click="picker = true">
          服务端目录…（高级）
        </button>
      </div>
      <textarea v-model="paths" rows="5"
        placeholder="D:\recon\subfinder.txt&#10;D:\recon\httpx.jsonl" />
      <FsPicker v-if="picker" @pick="addPath" @close="picker = false" />

      <div class="grid2">
        <div>
          <label>解析器（留空 = 自动仲裁）</label>
          <input v-model="parser" placeholder="domainlist / httpx" />
        </div>
        <div>
          <label>原文归档</label>
          <select v-model="archive">
            <option value="archived">archived（复制进数据区）</option>
            <option value="external">external（只记 locator）</option>
          </select>
        </div>
      </div>

      <label class="check">
        <input v-model="dryRun" type="checkbox" />
        dry-run（跑完不落库，只出报告）
      </label>

      <p v-if="error" class="bad small">{{ error }}</p>

      <ol v-if="phases.length" class="phases mono">
        <li v-for="(line, i) in phases" :key="i"
            :class="{ bad: line.level === 'error', warn: line.level === 'warn' }">
          {{ line.text }}
        </li>
      </ol>

      <div class="actions">
        <span class="dim small">
          {{ pathList.length }} 个路径
        </span>
        <span class="spacer" />
        <button class="ghost" @click="emit('close')">关闭</button>
        <button class="primary" :disabled="busy || uploading > 0" @click="submit">
          {{ busy ? '提交中…' : '开始导入' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.scrim { position: fixed; inset: 0; z-index: 50; background: var(--scrim);
         display: flex; align-items: center; justify-content: center; padding: 24px; }
.dialog { width: 560px; padding: 18px 20px; background: var(--panel-2);
          box-shadow: var(--raise); }
h1 { font-size: 16px; margin: 0 0 8px; }
label { display: block; margin: 10px 0 4px; color: var(--faint); font-size: 11px; }
.labelrow { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.labelrow label { margin: 10px 0 4px; }
.labelrow button { margin-top: 10px; }
label.check { display: flex; align-items: center; gap: 7px; margin-top: 12px;
              color: var(--dim); font-size: 12px; }
label.check input { width: auto; }
textarea, input, select { width: 100%; }
.drop {
  display: flex; align-items: center; gap: 10px;
  margin: 4px 0 6px; padding: 14px 12px;
  border: 1px dashed var(--line); border-radius: var(--radius-ui);
  background: var(--bg);
  transition: border-color .12s, background .12s;
}
.drop.over { border-color: var(--accent); background: var(--tint-accent); }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.small { font-size: 11px; line-height: 1.6; }
.phases { margin: 10px 0 0; padding: 8px 10px 8px 26px; max-height: 150px; overflow-y: auto;
          background: var(--bg); border: 1px solid var(--line); border-radius: 6px;
          font-size: 11px; }
.actions { display: flex; align-items: center; gap: 8px; margin-top: 14px; }
.spacer { flex: 1; }
</style>
