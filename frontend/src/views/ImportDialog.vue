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
import UniversalImport from '../components/UniversalImport.vue'
import ImportCoverage from '../components/ImportCoverage.vue'

const emit = defineEmits(['close'])

const paths = ref('')
const universalOpen = ref(false)
const universalReady = ref(null)
const reportNeedsReview = computed(() => (store.importReport?.report?.files || []).some(file =>
  ['partial', 'failed', 'needs_confirmation', 'cancelled'].includes(file.state) ||
  ['mapped_partial', 'unrecognized', 'record_error'].some(key => Number(file.import_coverage?.[key]) > 0)))
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
  if (universalOpen.value && (!universalReady.value || pathList.value.length !== 1 ||
      pathList.value[0] !== universalReady.value.path)) {
    error.value = '通用映射一次处理一个文件，请预览并确认映射。'
    return
  }
  if (!pathList.value.length) {
    error.value = '至少填一个文件或目录。'
    return
  }
  busy.value = true
  error.value = ''
  phases.value = []
  try {
    const record = await store.submitImport({
      paths: pathList.value, parser: universalOpen.value ? 'generic' : parser.value.trim(),
      mapping: universalOpen.value ? universalReady.value.mapping : undefined,
      expectedSha256: universalOpen.value ? universalReady.value.sha256 : undefined,
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

/**
 * SSE 批事件 → 界面（0.18.0 重写为**多文件**口径）。
 *
 * 多文件导入原来有三处"看起来内容丢了"的病，这里逐条对着治：
 *
 *   1. `progress` 的 records/line 是**逐文件**的 —— 直接覆盖会让第二个文件
 *      一开跑进度条归零。现在显示 `baseRecords + 当前文件进度`，而 base 只在
 *      `file_done` 里用数据层的 `counts.records` 累加 —— 界面仍然一个数都不
 *      自己数（2.7 那条纪律原样保留，只是"数"的口径从单文件换成整批）。
 *   2. `file_start` / `file_done` 原来落进 else，渲染成截断的 JSON —— 现在是
 *      显式的「[i/n] 文件名 状态」行。
 *   3. `done` 之后补拉 `GET /imports/{id}` 的报告（后端一直有，前端首次消费），
 *      逐文件 path/state/counts/job_id 渲染成完成态表 —— "哪个文件没成"从此
 *      看得见。
 *
 * else 兜底从「整段 JSON 截断 160 字符」改成「事件名 + 关键键」——兜底事件
 * （started / job_start 这些）本来就没有需要倒出来的载荷。
 */
function onBatch(payload) {
  if (payload.dropped) {
    store.importProgress.dropped += payload.dropped
    append(`（队列满，丢了 ${payload.dropped} 条事件 —— 丢帧不静默，但确实丢了）`,
      'warn')
  }
  for (const event of payload.events || []) {
    const data = event.data || {}
    const p = store.importProgress
    if (event.type === 'file_start') {
      p.fileIndex = (data.index ?? p.fileIndex - 1) + 1
      p.fileTotal = data.total ?? p.fileTotal
      append(`开始文件 [${p.fileIndex}/${p.fileTotal}] ${data.file || data.path || ''}`)
    } else if (event.type === 'progress') {
      // 进度**只**来自数据层的计数；累计 = 已完成文件的 base + 本文件进度
      p.records = p.baseRecords + (data.records ?? 0)
      p.line = data.line ?? p.line
      p.badLines = p.baseBadLines + ((data.counts && data.counts.bad_lines) ?? 0)
      p.stage = data.stage || p.stage
      // 坏行明细（流式、部分 —— 全量在数据区 reports/<job>.badlines.txt）
      for (const bad of (Array.isArray(data.bad_lines) ? data.bad_lines : [])) {
        if (p.badLinesDetail.length >= 1000) break
        p.badLinesDetail.push({ file: data.file || '', ...bad })
      }
      append(`进度 [${p.fileIndex}/${p.fileTotal}] ${data.file || ''}：` +
        `累计 ${p.records} 条记录，本文件 ${data.line} 行，坏行 ${p.badLines}`)
    } else if (event.type === 'file_done') {
      const counts = data.counts || {}
      p.baseRecords += Number(counts.records) || 0
      p.baseBadLines += Number(counts.bad_lines) || 0
      p.records = p.baseRecords           // 本文件已收尾，进度至此并入 base
      p.badLines = p.baseBadLines
      p.files.push({
        name: data.file || '', path: data.path || '', state: data.state || '',
        job_id: data.job_id ?? null, counts,
      })
      append(`文件完成 [${(data.index ?? 0) + 1}/${data.total ?? p.fileTotal}] ` +
        `${data.file || ''}：${data.state || ''} · ` +
        `${Number(counts.records) || 0} 条记录` +
        (counts.bad_lines ? ` · 坏行 ${counts.bad_lines}` : ''),
        data.state === 'done' ? 'info' : 'warn')
    } else if (event.type === 'job_done') {
      append(`作业 ${data.job_id} 结束：${data.state} ${JSON.stringify(data.counts || {})}`,
        data.state === 'done' ? 'info' : 'warn')
    } else if (event.type === 'done') {
      p.state = data.state
      append(`导入结束：${data.state}，job ${JSON.stringify(data.job_ids || [])}`,
        data.state === 'done' ? 'info' : 'warn')
      // 报告是补看的：拉不到只留一条 warn，进度与日志已经把这轮说完了
      store.loadImportReport(data.import_id)
      store.reload()
      store.loadMeta()
    } else if (event.type === 'error') {
      p.state = 'failed'
      append(`出错：${data.error}`, 'error')
    } else {
      p.stage = p.stage || event.type
      const KEYS = ['file', 'path', 'stage', 'job_id', 'parser', 'state']
      const bits = KEYS.filter((key) => data[key] !== undefined && data[key] !== null)
        .map((key) => `${key}=${data[key]}`)
      append(bits.length ? `${event.type} ${bits.join(' ')}` :
        `${event.type}（${Object.keys(data).join(', ') || '无参数'}）`)
    }
  }
}
</script>

<template>
  <div class="scrim" @click.self="emit('close')">
    <div class="dialog panel">
      <h1>导入</h1>
      <p class="dim small">
        导入资产侦察与流量数据（支持 subfinder / httpx / nuclei / katana / gau、含 JS 响应正文的 HAR 抓包及带 URL 的 JS 清单等）。首选：点「选择文件…」或直接把文件拖进来。大文件可用「服务端目录…」点选，或直接手填绝对路径。陌生工具文件请勾选下方通用字段映射。
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
        placeholder="D:\recon\subfinder.txt&#10;D:\recon\httpx.jsonl&#10;D:\recon\traffic.har&#10;D:\recon\app.manifest.json" />
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
        <input v-model="universalOpen" type="checkbox" @change="universalReady = null" />
        陌生工具 / 自定义字段：先预览与确认映射
      </label>
      <UniversalImport v-if="universalOpen" :path="pathList.length === 1 ? pathList[0] : ''"
                       @ready="universalReady = $event" />
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

      <!--
        完成态报告（0.18.0）：`done` 之后拉 `GET /imports/{id}` 的 report ——
        逐文件 path/state/counts/job_id。"三个文件哪个没成"只能看这里：
        事件流是一条时间线，报告才是按文件归档的那份。
      -->
      <div v-if="store.importReport && store.importReport.report" class="report">
        <p v-if="reportNeedsReview" class="warn" role="status">
          处理已结束，但仍有错误、未识别或未完整映射的数据。请检查逐文件状态与覆盖明细；任务结束不代表数据全部理解。
        </p>
        <div class="labelrow">
          <label>导入报告 · {{ store.importReport.state }}</label>
        </div>
        <table class="rpt">
          <thead>
            <tr><th>文件</th><th>状态</th><th>记录</th><th>坏行</th><th>job</th></tr>
          </thead>
          <tbody>
            <tr v-for="(f, i) in store.importReport.report.files || []" :key="i"
                :class="{ bad: f.state === 'failed' || f.state === 'cancelled',
                          warn: f.state === 'partial' }">
              <td class="mono" :title="f.path">{{ f.path }}</td>
              <td>{{ f.state }}</td>
              <td class="mono">{{ (f.counts || {}).records ?? '—' }}</td>
              <td class="mono">{{ (f.counts || {}).bad_lines ?? '—' }}</td>
              <td class="mono">{{ f.job_id ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
        <template v-for="(f, i) in store.importReport.report.files || []" :key="`coverage-${i}`">
          <ImportCoverage v-if="f.import_coverage" :coverage="f.import_coverage" />
        </template>
        <details v-if="(store.importReport.report.discovery_skipped || []).length" class="warn small">
          <summary>目录扫描跳过 {{ store.importReport.report.discovery_skipped.length }} 个文件（未执行导入）</summary>
          <p v-for="(item, i) in store.importReport.report.discovery_skipped.slice(0, 200)" :key="i">
            {{ item.path }} · {{ item.reason }}
          </p>
          <p v-if="store.importReport.report.discovery_skipped.length > 200">仅显示前 200 项，完整清单在导入报告中。</p>
        </details>
        <p v-if="(store.importReport.problems || []).length" class="warn small">
          提示：{{ store.importReport.problems.join('；') }}
        </p>
        <p v-if="(store.importReport.error || '').length" class="bad small">
          {{ store.importReport.error }}
        </p>
      </div>

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
.dialog { width: 560px; max-height: calc(100vh - 48px); overflow-y: auto;
          padding: 18px 20px; background: var(--panel-2);
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
.report { margin-top: 10px; }
.rpt { width: 100%; border-collapse: collapse; font-size: 11px; }
.rpt th { text-align: left; color: var(--faint); font-weight: 500;
          padding: 3px 6px; border-bottom: 1px solid var(--line);
          font-size: 10px; text-transform: uppercase; letter-spacing: .04em; }
.rpt td { padding: 3px 6px; border-bottom: 1px solid var(--line-soft);
          word-break: break-all; }
.rpt td:first-child { max-width: 240px; }
.rpt tr.bad td { color: var(--bad); }
.rpt tr.warn td { color: var(--warn); }
.actions { display: flex; align-items: center; gap: 8px; margin-top: 14px; }
.spacer { flex: 1; }
</style>
