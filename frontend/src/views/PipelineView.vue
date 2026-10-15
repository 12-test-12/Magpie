<script setup>
/**
 * 编排模式（`view === 'pipeline'`）—— 计划书 §7.2 的第二块画布。
 *
 * 这一页是**外壳**：工具栏 + 画布 + 步骤面板 + 运行历史。四块各自守一条冻结项：
 *
 *   1. **画布可编辑只在编排模式**。真正的编辑入口全部落在这里的按钮与输入框上
 *      （表单动作），画布手势只表达依赖（边）——
 *      `PipelineCanvas.vue` 的文件头写了为什么。
 *   2. **可保存**：`POST /pipelines` / `PUT /pipelines/{id}`，保存时带 `revision`
 *      （乐观锁）。"未保存"标记来自一次**深比较**（`store.pipelineDirty`），
 *      不是手维护的布尔位。
 *   3. **可复用**：`base_dir` / `data_dir` 可以在**这一次跑**上覆盖 —— 同一份
 *      定义换一个目录再跑，就是 §4.5 的"换项目 / 换路径再跑"。
 *   4. **可批量跑**：一次 run = 1 行 `pipeline_runs` + N 行 job，进度走 SSE。
 *
 * ⚠️ 这里**不发明第二套导入 / undo 语义**：运行调的是 `POST /pipelines/{id}/run`
 * （落到既有的 `ingest`），撤销调的是 `POST /runs/{id}/undo`（落到既有的 `job`
 * 粒度 undo）。界面上多出来的只有"一次跑了很多作业"这个**分组**。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import PipelineCanvas from '../components/PipelineCanvas.vue'
import { store } from '../store.js'
import { AUTHORITY_LEVELS, SOURCE_KINDS, stepById } from '../pipeline.js'

const dryRun = ref(false)
const baseDir = ref('')
const dataDir = ref('')
const preview = ref(null)
const busy = ref(false)
const error = ref('')
let closeStream = null

const pipeline = computed(() => store.pipeline)
const steps = computed(() => (pipeline.value.definition || {}).steps || [])
const selected = computed(() => stepById(pipeline.value.definition,
                                         pipeline.value.selectedStepId))
const parsers = computed(() => ((store.meta || {}).parsers || [])
  .map((item) => item.tool))

function run(fn) {
  busy.value = true
  error.value = ''
  return Promise.resolve(fn())
    .catch((err) => { error.value = err.message })
    .finally(() => { busy.value = false })
}

onMounted(() => {
  if (!pipeline.value.list.length) store.loadPipelines()
  if (pipeline.value.definition === null) store.newPipelineDraft()
})

watch(() => store.project, async () => {
  await store.loadPipelines()
  if (pipeline.value.definition === null) store.newPipelineDraft()
})

onUnmounted(() => {
  if (closeStream) closeStream()
})

async function save() {
  await run(async () => {
    const saved = await store.savePipeline()
    if (saved) preview.value = null
  })
}

async function previewRun() {
  await run(async () => {
    preview.value = null
    const data = await store.runPipeline({
      dryRun: true,
      baseDir: baseDir.value || null,
      dataDir: dataDir.value || null,
    })
    preview.value = data
  })
}

async function realRun() {
  await run(async () => {
    preview.value = null
    const data = await store.runPipeline({
      dryRun: false,
      baseDir: baseDir.value || null,
      dataDir: dataDir.value || null,
    })
    if (data && data.run_id !== null) {
      if (closeStream) closeStream()
      closeStream = store.watchPipelineRun(data.run_id)
    }
  })
}

async function undo(runId) {
  await run(() => store.undoRun(runId, false))
  await store.loadPipelineRuns(pipeline.value.id)
}

async function redo(runId) {
  await run(() => store.redoRun(runId, false))
  await store.loadPipelineRuns(pipeline.value.id)
}
</script>

<template>
  <div class="pipeline">
    <div class="bar">
      <input
        class="name"
        :value="(pipeline.definition || {}).name || ''"
        placeholder="这条管线叫什么"
        @input="store.patchDefinition({ name: $event.target.value })"
      />
      <span v-if="store.pipelineDirty" class="dirty" title="草稿与已保存的那一份不同">
        未保存
      </span>
      <span v-else-if="pipeline.id" class="saved dim">已保存 · revision {{ pipeline.revision }}</span>
      <span class="spacer" />
      <button class="ghost" :disabled="busy" @click="store.newPipelineDraft()">新建</button>
      <button :disabled="busy || !pipeline.definition" @click="save">
        {{ pipeline.id === null ? '保存为新定义' : '保存' }}
      </button>
      <button class="ghost" :disabled="busy || pipeline.id === null"
              @click="store.archivePipeline(pipeline.id)">归档</button>
      <button class="ghost" :disabled="busy || pipeline.id === null" @click="previewRun">
        预演
      </button>
      <button class="go" :disabled="busy || pipeline.id === null" @click="realRun">运行</button>
    </div>

    <div v-if="error" class="error">{{ error }}</div>

    <div class="split">
      <div class="canvas-wrap">
        <PipelineCanvas>
          <template #empty>
            <p class="dim">
              这条定义还没有步骤。左边点「＋ 步骤」开始 ——
              加步骤是**表单动作**，连线才是画布手势。
            </p>
          </template>
        </PipelineCanvas>
        <div v-if="pipeline.activeRunId && pipeline.progress.total" class="run-progress">
          run {{ pipeline.activeRunId }}：
          {{ pipeline.progress.done }} / {{ pipeline.progress.total }} 个文件
          <span v-if="pipeline.progress.file" class="mono dim">
            （{{ pipeline.progress.file }}）
          </span>
        </div>
      </div>

      <aside class="side">
        <section>
          <h2 class="section">步骤</h2>
          <ul class="steps">
            <li
              v-for="step in steps"
              :key="step.id"
              :class="{ active: String(pipeline.selectedStepId) === String(step.id) }"
              @click="store.selectStep(step.id)"
            >
              <span class="mono">{{ step.id }}</span>
              <span class="dim mono ell">{{ (step.source || {}).pattern || '—' }}</span>
              <button class="x" title="删掉这一步（同时清掉别人对它的引用）"
                      @click.stop="store.removeStep(step.id)">×</button>
            </li>
            <li v-if="!steps.length" class="dim empty">还没有步骤</li>
          </ul>
          <button class="ghost add" @click="store.addStep()">＋ 步骤</button>
        </section>

        <section v-if="selected">
          <h2 class="section">这一步</h2>
          <label class="field">
            <span>来源类型</span>
            <select :value="selected.source.kind"
                    @change="store.patchStep(selected.id, { source: {
                      kind: $event.target.value, pattern: selected.source.pattern } })">
              <option v-for="kind in SOURCE_KINDS" :key="kind.value" :value="kind.value">
                {{ kind.label }}
              </option>
            </select>
          </label>
          <label class="field">
            <span>{{ selected.source.kind === 'file' ? '文件名' :
                    (selected.source.kind === 'dir' ? '目录' : '通配模式') }}</span>
            <input class="mono" :value="selected.source.pattern"
                   placeholder="recon/*.txt"
                   @input="store.patchStep(selected.id, { source: {
                     kind: selected.source.kind, pattern: $event.target.value } })" />
          </label>
          <label class="field">
            <span>来源名（写进 sources 表）</span>
            <input class="mono" :value="selected.source_name || ''"
                   placeholder="subfinder"
                   @input="store.patchStep(selected.id,
                     { source_name: $event.target.value || null })" />
          </label>
          <label class="field">
            <span>解析器（留空 = 交给嗅探仲裁）</span>
            <select :value="selected.parser || ''"
                    @change="store.patchStep(selected.id,
                      { parser: $event.target.value || null })">
              <option value="">自动仲裁</option>
              <option v-for="tool in parsers" :key="tool" :value="tool">{{ tool }}</option>
            </select>
          </label>
          <label class="field">
            <span>权威级别</span>
            <select :value="selected.authority"
                    @change="store.patchStep(selected.id, { authority: $event.target.value })">
              <option v-for="level in AUTHORITY_LEVELS" :key="level" :value="level">
                {{ level }}
              </option>
            </select>
          </label>
          <label class="check">
            <input type="checkbox" :checked="selected.dry_run"
                   @change="store.patchStep(selected.id, { dry_run: $event.target.checked })" />
            <span>这一步只预演（dry_run）</span>
          </label>

          <h3 class="sub">依赖（画布上连过来的线）</h3>
          <ul class="deps">
            <li v-for="dep in selected.after" :key="dep">
              <span class="mono">{{ dep }}</span>
              <button class="x" title="断开这条依赖"
                      @click="store.disconnectSteps(dep, selected.id)">×</button>
            </li>
            <li v-if="!selected.after.length" class="dim empty">没有依赖（它是起点）</li>
          </ul>
        </section>

        <section>
          <h2 class="section">这一次跑</h2>
          <label class="field">
            <span>基准目录（留空 = 定义里的 base_dir）</span>
            <input class="mono" v-model="baseDir"
                   :placeholder="(pipeline.saved || {}).base_dir || ''" />
          </label>
          <label class="field">
            <span>数据目录（留空 = 定义里的 data_dir）</span>
            <input class="mono" v-model="dataDir"
                   :placeholder="(pipeline.saved || {}).data_dir || ''" />
          </label>
          <p class="dim note">
            覆盖只作用于**这一次跑**。定义一个字不改 —— 跑是读定义、写事实。
          </p>
        </section>

        <section v-if="preview">
          <h2 class="section">预演结果</h2>
          <ul class="totals">
            <li v-for="(value, key) in preview.report.totals" :key="key">
              <span class="dim">{{ key }}</span><span class="mono">{{ value }}</span>
            </li>
          </ul>
          <p class="dim note">预演一行都没写库（冻结 F10：干跑不建 run 行）。</p>
        </section>

        <section>
          <h2 class="section">运行历史</h2>
          <ul class="runs">
            <li v-for="item in pipeline.runs" :key="item.id">
              <span class="mono">#{{ item.id }}</span>
              <span :class="item.state">{{ item.state }}</span>
              <span class="dim mono">{{ item.job_count }} 个作业</span>
              <span class="dim mono">{{ item.applied ? '生效中' : '已撤销' }}</span>
              <button v-if="item.can_undo" class="ghost x" :disabled="busy"
                      @click="undo(item.id)">撤销</button>
              <button v-else class="ghost x" :disabled="busy"
                      @click="redo(item.id)">复原</button>
            </li>
            <li v-if="!pipeline.runs.length" class="dim empty">还没跑过</li>
          </ul>
        </section>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.pipeline { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--line);
  background: var(--panel);
}
.name { flex: 0 1 260px; }
.dirty { color: var(--warn); font-size: 11px; }
.saved { font-size: 11px; }
.spacer { flex: 1; }
.go { border-color: var(--accent); color: var(--accent); }
.error {
  padding: 6px 12px;
  color: var(--bad);
  font-size: 12px;
  border-bottom: 1px solid var(--line);
}
.split { flex: 1; display: flex; min-height: 0; }
.canvas-wrap { position: relative; flex: 1; min-width: 0; }
.run-progress {
  position: absolute;
  bottom: 12px;
  left: 12px;
  z-index: 4;
  padding: 4px 10px;
  border: 1px solid var(--accent);
  border-radius: var(--radius-ui);
  background: var(--overlay);
  font-size: 11px;
}
.side {
  width: 288px;
  flex: none;
  border-left: 1px solid var(--line);
  background: var(--panel);
  padding: 12px;
  overflow-y: auto;
}
section + section { margin-top: 16px; }
.list-reset, .steps, .deps, .runs, .totals { list-style: none; margin: 0; padding: 0; }
.steps li, .runs li {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 6px;
  border-radius: var(--radius-ui);
  font-size: 12px;
  cursor: pointer;
}
.steps li:hover { background: var(--tint-hover); }
.steps li.active { background: var(--tint-active); color: var(--accent); }
.steps .ell { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.steps .x, .deps .x, .runs .x { padding: 0 5px; line-height: 1.4; }
.empty { cursor: default; font-size: 11px; }
.add { width: 100%; margin-top: 6px; }
.field { display: flex; flex-direction: column; gap: 3px; margin-bottom: 8px; }
.field span { font-size: 11px; color: var(--dim); }
.check { display: flex; align-items: center; gap: 6px; font-size: 12px; }
.sub { font-size: 11px; color: var(--dim); margin: 10px 0 4px; }
.deps li, .totals li, .runs li { display: flex; align-items: center; gap: 6px; font-size: 12px; }
.totals li { justify-content: space-between; }
.note { font-size: 11px; line-height: 1.5; margin: 6px 0 0; }
.runs .done { color: var(--ok); }
.runs .failed { color: var(--bad); }
.runs .partial { color: var(--warn); }
</style>
