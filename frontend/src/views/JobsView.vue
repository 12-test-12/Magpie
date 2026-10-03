<script setup>
/**
 * 作业历史 + undo / redo（2.8 承接 CLI 后置移交的三个入口之一）。
 *
 * 这一页是 **G2 出口 Gate 第 ② 条**（"导入 → undo → redo 精确复原"）在界面上
 * 的验收面。它做三件事，一件都不能少：
 *
 *   1. **列出作业**：`GET /jobs`。每个作业的 `can_undo` / `can_redo` 由数据层
 *      的 `applied` 决定 —— 界面**不自己记账**。界面自己记的状态迟早会和库里
 *      的不一致，而那时用户看到的是一个说"已撤销"、实际没撤销的按钮。
 *   2. **干跑预览**：`POST /jobs/{id}/undo {dry_run:true}`。干跑与真跑**输出
 *      同构**，所以下面这段渲染代码同时服务两者 —— 这正是数据层让 dry_run 与
 *      真跑同构的**用途**，不是为了好看。
 *   3. **explain_job_scope**：`GET /jobs/{id}` 里原样带回来的。旧作业必须用
 *      **当时的** scope 解释，界面拿当前 scope 去算会得出"这条覆盖失效了"的
 *      假结论 —— `interpret_with` 就在那里摆着，照着显示。
 *
 * 「先干跑、再真跑」不是多一步：undo 的 impact 是"这次会撤回多少条贡献"，
 * 而这个数字在按下去之前是**可以**知道的。让人在按之前看到它，与按完再被告知，
 * 是完全不同的两件事。
 */
import { computed, onMounted, ref, watch } from 'vue'
import VirtualTable from '../components/VirtualTable.vue'
import EmptyState from '../components/EmptyState.vue'
import { store } from '../store.js'

/** 空态 CTA「导入一个文件」冒给外壳开导入对话框。 */
const emit = defineEmits(['import'])

const rows = ref([])
const selected = ref(null)
const detail = ref(null)
const busy = ref(false)
const error = ref('')
const preview = ref(null)

const COLUMNS = [
  { key: 'id', label: 'job', mono: true, width: '52px' },
  { key: 'kind', label: '类型', width: '64px' },
  { key: 'state', label: '状态', width: '68px' },
  { key: 'appliedLabel', label: '生效', width: '56px' },
  { key: 'contributions', label: '贡献', mono: true, width: '60px' },
  { key: 'retracted', label: '已撤回', mono: true, width: '66px' },
  { key: 'started_at', label: '开始', mono: true },
  { key: 'actor', label: 'actor', width: '56px' },
]

const STATE_LABEL = {
  running: '执行中', done: '完成', failed: '失败',
  partial: '部分', aborted: '中止',
}

async function load() {
  error.value = ''
  try {
    const data = await store.listJobs(200)
    rows.value = data.map((item) => Object.assign({}, item, {
      __key: item.id,
      appliedLabel: item.applied ? '生效中' : '已撤销',
      state: STATE_LABEL[item.state] || item.state,
    }))
    if (selected.value) {
      const still = rows.value.find((r) => r.id === selected.value.id)
      if (still) await pick(still, { keepPreview: true })
    }
  } catch (err) {
    error.value = err.message
    rows.value = []
  }
}

async function pick(row, { keepPreview = false } = {}) {
  selected.value = row
  if (!keepPreview) preview.value = null
  try {
    detail.value = await store.jobDetail(row.id)
  } catch (err) {
    detail.value = null
    error.value = err.message
  }
}

/** 干跑。**它不改任何数据** —— 报告与真跑同构，差别只在 COMMIT→ROLLBACK。 */
async function dryRun(op) {
  if (!selected.value) return
  busy.value = true
  error.value = ''
  try {
    const call = op === 'undo' ? store.undoJob : store.redoJob
    preview.value = { op, dry_run: true, data: await call(selected.value.id, true) }
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = false
  }
}

async function applyOp(op) {
  if (!selected.value) return
  busy.value = true
  error.value = ''
  try {
    const call = op === 'undo' ? store.undoJob : store.redoJob
    const data = await call(selected.value.id, false)
    preview.value = { op, dry_run: false, data }
    store.pushLog(`job ${selected.value.id} ${op} 完成：` +
      `撤回 ${data.report?.impact?.supersessions_added ?? 0} 条贡献`,
      'import', 'info')
    await load()
    await store.reload()                    // 画布跟着变 —— undo 的效果必须看得见
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = false
  }
}

const impact = computed(() => {
  const report = preview.value?.data?.report
  if (!report) return null
  const raw = report.impact || {}
  return Object.entries(raw).filter(([, value]) => typeof value !== 'object')
})

const blocked = computed(() => preview.value?.data?.report?.impact?.blocked || [])

onMounted(load)
watch(() => store.view, (value) => {
  if (value === 'jobs') load()
})
</script>

<template>
  <div class="jobs">
    <div class="head">
      <h2 class="section">作业历史（最近 200 条）</h2>
      <span class="spacer" />
      <button class="ghost" :disabled="busy" @click="load">刷新</button>
    </div>

    <p v-if="error" class="bad small">{{ error }}</p>

    <div class="body">
      <div class="table-wrap">
        <VirtualTable
          :rows="rows"
          :columns="COLUMNS"
          :selected-key="selected ? selected.id : null"
          empty-text="还没有作业。"
          @row-click="pick"
        >
          <template #empty>
            <EmptyState
              title="还没有作业"
              desc="每次导入每个文件产生一条作业 —— 先导入一个文件，这里就有了。"
            >
              <button class="primary" @click="emit('import')">导入一个文件</button>
            </EmptyState>
          </template>
          <template #appliedLabel="{ row }">
            <span :class="row.applied ? 'ok' : 'warn'">{{ row.appliedLabel }}</span>
          </template>
        </VirtualTable>
      </div>

      <aside class="detail panel">
        <p v-if="!selected" class="dim pad">
          选一条作业。这里能对任一作业执行**撤销 / 复原**，并且可以先看干跑预览 ——
          预览与真跑的输出是同构的（数据层保证），只有 COMMIT 与 ROLLBACK 的差别。
        </p>

        <template v-else>
          <h2 class="section">job {{ selected.id }}</h2>
          <dl class="kv">
            <dt>状态</dt><dd>{{ selected.state }} · {{ selected.appliedLabel }}</dd>
            <dt>贡献</dt><dd class="mono">{{ selected.contributions }} 条，已撤回
              {{ selected.retracted }} 条</dd>
            <dt>覆盖</dt><dd class="mono">{{ selected.coverage }}
              · scope v{{ selected.scope_version }}</dd>
            <dt>actor</dt><dd class="mono">{{ selected.actor }}</dd>
            <dt>开始</dt><dd class="mono">{{ selected.started_at }}</dd>
          </dl>

          <div class="actions">
            <button class="ghost" :disabled="busy || !selected.can_undo"
                    @click="dryRun('undo')">干跑撤销</button>
            <button class="ghost" :disabled="busy || !selected.can_redo"
                    @click="dryRun('redo')">干跑复原</button>
            <button class="primary" :disabled="busy || !selected.can_undo"
                    @click="applyOp('undo')">撤销</button>
            <button class="primary" :disabled="busy || !selected.can_redo"
                    @click="applyOp('redo')">复原</button>
          </div>

          <!--
            explain_job_scope 的输出。**旧作业用当时的 scope 解释** —— 这一行
            存在的全部意义就是防止有人拿当前 scope 去算历史作业的覆盖。
          -->
          <template v-if="detail">
            <h2 class="section" style="margin-top:14px">scope 解释（explain_job_scope）</h2>
            <dl class="kv">
              <dt>用什么解释</dt>
              <dd :class="detail.scope.interpret_with === 'historical_scope' ? 'warn' : 'ok'">
                {{ detail.scope.interpret_with === 'historical_scope'
                  ? '当时的 scope（历史快照）' : '当前 scope' }}
              </dd>
              <dt>落后版本</dt><dd class="mono">{{ detail.scope.scope_versions_behind }}</dd>
              <dt>解析器漂移</dt>
              <dd :class="detail.scope.parser_drift ? 'warn' : ''">
                {{ detail.scope.parser_drift ? '有（fingerprint 与当前不一致）' : '无' }}
              </dd>
              <dt>可用于判 inactive</dt>
              <dd class="mono">{{ detail.scope.coverage_usable_for_inactive ? '是' : '否' }}</dd>
              <dt>作业时 fingerprint</dt>
              <dd class="mono tiny">{{ detail.scope.job_parser_fingerprint || '—' }}</dd>
            </dl>
          </template>

          <template v-if="preview">
            <h2 class="section" style="margin-top:14px">
              {{ preview.dry_run ? '干跑预览（库未变）' : '执行结果' }} ·
              {{ preview.op }}
            </h2>
            <dl class="kv">
              <dt v-for="[key, value] in impact" :key="key">
                <span class="mono">{{ key }}</span>
              </dt>
              <dd v-for="[key, value] in impact" :key="'v' + key" class="mono">
                {{ value }}
              </dd>
            </dl>
            <p v-if="blocked.length" class="warn small">
              {{ blocked.length }} 处被阻塞（数据层判定不可撤销）—— 那不是失败，
              是"这次撤销会破坏不变量，所以它拒绝了"。
            </p>
            <p v-if="preview.data.report.noop" class="dim small">
              幂等：这个作业已经是目标状态，什么都不会发生。
            </p>
          </template>

          <h2 class="section" style="margin-top:14px">活动</h2>
          <ul class="acts">
            <li v-for="row in (detail ? detail.activity : [])" :key="row.id">
              <span class="mono action">{{ row.action }}</span>
              <span class="faint">{{ row.actor }}</span>
            </li>
            <li v-if="detail && !detail.activity.length" class="dim">（无）</li>
          </ul>
        </template>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.jobs { display: flex; flex-direction: column; height: 100%; padding: 12px; gap: 8px; }
.head { display: flex; align-items: center; }
.spacer { flex: 1; }
.body { flex: 1; display: flex; gap: 12px; min-height: 0; }
.table-wrap { flex: 1; min-width: 0; }
.detail { width: 360px; flex: none; overflow-y: auto; padding: 12px; }
.pad { padding: 6px; font-size: 12px; line-height: 1.6; }
.small { font-size: 11px; line-height: 1.5; }
.tiny { font-size: 10px; word-break: break-all; }
.kv { display: grid; grid-template-columns: 92px 1fr; gap: 3px 8px; margin: 0 0 10px;
      font-size: 11px; }
.kv dt { color: var(--faint); }
.kv dd { margin: 0; word-break: break-all; }
.actions { display: flex; flex-wrap: wrap; gap: 6px; }
.acts { list-style: none; margin: 0; padding: 0; font-size: 11px; }
.acts li { display: flex; gap: 7px; padding: 2px 0; }
.acts .action { color: var(--dim); }
</style>
