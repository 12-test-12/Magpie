<script setup>
/**
 * 数据体检页（计划书 §7.4）。
 *
 * 计划书要的那几项里，**数据层真的有**的是这些（不是界面自己编的指标）：
 *
 *   `store.integrity_check`  → `violations` / `foreign_key_check` / `sqlite_integrity`
 *   `jobs_by_state`          → 每个执行状态有多少作业
 *   `open_inbox` / `raw_files.storage_state` → 待裁决数与原文持有状态
 *   `untested_entities`      → **一条观测都没有的实体**
 *
 * 计划书还列了"孤儿实体、低置信度、未知字段、越界资产、归档体积、距上次备份
 * 天数、解析器夹具健康度"—— 这些**今天没有对应的端点**。这一页把它们列在
 * 「尚未提供」里，而不是用近似指标顶上去：一个假的"越界资产 0"比不显示更糟，
 * 它会被当成"查过了，没问题"。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { get } from '../api.js'
import { store } from '../store.js'

const report = ref(null)
const loading = ref(false)
const error = ref('')

const STATE_LABEL = {
  running: '执行中', done: '完成', failed: '失败',
  partial: '部分', aborted: '中止',
}

const NOT_PROVIDED = [
  { item: '低置信度观测', why: '需要按 confidence 聚合的端点' },
  { item: '未知字段', why: '需要 `field_registry` 与漂移检测（阶段 3）' },
  { item: '越界资产', why: '需要 scope 求值（2.6/§8 的 in_scope）' },
  { item: '归档体积', why: '可由 raw_files 的 byte_count 汇总，尚未做' },
  { item: '距上次备份天数', why: '备份本身还不是这个工具的功能' },
  { item: '解析器夹具健康度', why: '需要夹具回归入口（阶段 3）' },
]

const healthy = computed(() => report.value && report.value.ok &&
  !report.value.violations.length && !report.value.foreign_key_check.length)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const query = store.project ? `?project=${encodeURIComponent(store.project)}` : ''
    report.value = await get(`/doctor${query}`)
  } catch (err) {
    error.value = err.message
    report.value = null
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(() => store.project, load)
watch(() => store.view, (value) => {
  if (value === 'doctor') load()
})
</script>

<template>
  <div class="doctor">
    <header class="head">
      <h2 class="section">数据体检</h2>
      <span v-if="report && healthy" class="chip ok">不变量全部成立</span>
      <span v-else-if="report" class="chip bad">
        {{ report.violations.length }} 条不变量违规</span>
      <span class="spacer" />
      <button class="ghost" :disabled="loading" @click="load">刷新</button>
    </header>

    <p v-if="error" class="bad small">{{ error }}</p>
    <p v-else-if="loading && !report" class="dim">体检中…</p>

    <div v-if="report" class="cols">
      <section class="panel card">
        <h2 class="section">完整性</h2>
        <dl class="kv">
          <dt>总体</dt><dd :class="report.ok ? 'ok' : 'bad'">
            {{ report.ok ? '通过' : '未通过' }}</dd>
          <dt>不变量违规</dt>
          <dd :class="report.violations.length ? 'bad' : 'ok'">
            {{ report.violations.length }}</dd>
          <dt>外键检查</dt>
          <dd :class="report.foreign_key_check.length ? 'bad' : 'ok'">
            {{ report.foreign_key_check.length }}</dd>
          <dt>sqlite integrity</dt>
          <dd class="mono">{{ report.sqlite_integrity || '（无输出）' }}</dd>
        </dl>
        <ul v-if="report.violations.length" class="violations mono">
          <li v-for="(v, i) in report.violations" :key="i">{{ v }}</li>
        </ul>
      </section>

      <section class="panel card">
        <h2 class="section">作业 / 工作层</h2>
        <dl class="kv">
          <dt>待裁决</dt>
          <dd :class="report.open_inbox ? 'warn' : 'ok'">{{ report.open_inbox }}</dd>
          <dt>没测过的实体</dt>
          <dd :class="report.untested_entities ? 'warn' : 'ok'">
            {{ report.untested_entities ?? '（未给 project，无法算）' }}</dd>
        </dl>
        <div class="sub">
          <span class="faint">作业按状态</span>
          <span v-for="row in report.jobs_by_state" :key="row.state" class="chip">
            {{ STATE_LABEL[row.state] || row.state }} <b>{{ row.n }}</b>
          </span>
          <span v-if="!report.jobs_by_state.length" class="faint">（还没有作业）</span>
        </div>
        <div class="sub">
          <span class="faint">原文持有</span>
          <span v-for="row in report.raw_files" :key="row.storage_state" class="chip">
            {{ row.storage_state }} <b>{{ row.n }}</b>
          </span>
          <span v-if="!report.raw_files.length" class="faint">（无原文）</span>
        </div>
      </section>

      <section class="panel card">
        <h2 class="section">尚未提供（不假装有）</h2>
        <ul class="missing">
          <li v-for="row in NOT_PROVIDED" :key="row.item">
            <span class="warn">{{ row.item }}</span>
            <span class="faint">{{ row.why }}</span>
          </li>
        </ul>
      </section>
    </div>
  </div>
</template>

<style scoped>
.doctor { display: flex; flex-direction: column; height: 100%; padding: 12px; gap: 8px;
          overflow-y: auto; }
.head { display: flex; align-items: center; gap: 8px; }
.spacer { flex: 1; }
.cols { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; }
.card { padding: 12px; }
.kv { display: grid; grid-template-columns: 118px 1fr; gap: 3px 8px; margin: 0; font-size: 12px; }
.kv dt { color: var(--faint); }
.kv dd { margin: 0; word-break: break-all; }
.violations { margin: 8px 0 0; padding-left: 16px; font-size: 11px; color: var(--bad); }
.sub { margin-top: 10px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
       font-size: 11px; }
.missing { list-style: none; margin: 0; padding: 0; font-size: 11px; }
.missing li { display: flex; gap: 8px; padding: 3px 0; }
.missing li span:first-child { min-width: 110px; }
.small { font-size: 11px; }
</style>
