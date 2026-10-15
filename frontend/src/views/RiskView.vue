<script setup>
/**
 * 风险评分与攻击面概览页（5.5 / LARI-45）。
 *
 * 这一页**自己不判任何东西**：总分、逐因子分解、攻击面聚合全部来自
 * `GET /api/v1/risk`（背后是 `core/risk.overview`，`magpie risk` 调同一段）——
 * 与 DoctorView 同一条纪律：任何"在这里再算一次"的写法都会凭空造出第二份
 * 真相，而两个真相不会同时出错、也不会被人一眼看穿。
 *
 * 渲染上的三个取舍：
 *
 *   * **因子行只铺非零的**：13 个因子里 9 个是 0 时，铺满一屏 0 等于把真正要紧的
 *     两行挤出视野 —— 0 们在"摘要"里各有归属，不丢信息；
 *   * **每行带首条依据**（evidence 的 label）：这一页要能答"为什么是这个分"，
 *     一个没有依据列的因子表就是黑盒总分，正是本步的失败模式；
 *   * **未映射漏洞显眼报警**：`found_on` 落点缺失的漏洞不在任何主机行上，
 *     藏起来会让攻击面看起来比实际完整。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { get } from '../api.js'
import { store } from '../store.js'

const payload = ref(null)
const loading = ref(false)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    const query = store.project ? `?project=${encodeURIComponent(store.project)}` : ''
    payload.value = await get(`/risk${query}`)
  } catch (err) {
    error.value = err.message
    payload.value = null
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(() => store.project, load)
watch(() => store.view, (value) => {
  if (value === 'risk') load()
})

const score = computed(() => payload.value && payload.value.score)
const facts = computed(() => payload.value && payload.value.facts)
const surface = computed(() => payload.value && payload.value.attack_surface)

//: 只铺非零因子；条形宽度按最大贡献归一（0 分时兜底 100，避免除零）。
const hitFactors = computed(() => {
  if (!score.value) return []
  return score.value.factors.filter((f) => f.count > 0)
})
const maxContribution = computed(() => {
  const list = hitFactors.value
  return list.length ? Math.max(...list.map((f) => f.contribution)) : 1
})
const unmapped = computed(() => {
  if (!surface.value || !surface.value.totals) return []
  return surface.value.totals.unmapped_open_vulns || []
})

//: `vuln_by_state` 的直排（open 6、fixed 2…）—— 分布是后端算好的，这里只拼文字。
const stateText = computed(() => {
  if (!facts.value) return ''
  const entries = Object.entries(facts.value.vuln_by_state || {})
  return entries.length ? entries.map(([k, v]) => `${k} ${v}`).join('、') : '无'
})

//: 一行因子的依据：首条 label + "等 N 条"（evidence 最多展开 50 条，
//: 计数以 evidence_total 为准 —— 与 CLI --human 同一个口径）。
function evidenceText(factor) {
  const first = factor.evidence && factor.evidence[0]
  if (!first) return ''
  const rest = factor.evidence_total > 1 ? ` 等 ${factor.evidence_total} 条` : ''
  return `${first.label}${rest}`
}

function hostServices(row) {
  return (row.services || []).map((s) => (s.products && s.products.length
    ? `${s.key}（${s.products.join('、')}）` : s.key)).join('、')
}
</script>

<template>
  <div class="risk">
    <header class="head">
      <h2 class="section">风险评分</h2>
      <template v-if="score">
        <span class="total mono">{{ score.total }}</span>
        <span class="chip" :title="payload.rules_digest">
          规则 v{{ payload.rule_version }} · {{ payload.rules_digest.slice(0, 18) }}…
        </span>
        <span v-if="score.total === 0" class="chip ok">零命中 —— 这是结果，不是错误</span>
      </template>
      <span class="spacer" />
      <button class="ghost" :disabled="loading" @click="load">刷新</button>
    </header>

    <p v-if="error" class="bad small">{{ error }}</p>
    <p v-else-if="loading && !payload" class="dim">评分中…</p>

    <div v-if="payload" class="cols">
      <section class="panel card wide">
        <h2 class="section">逐因子分解（合计 = 总分）</h2>
        <p class="faint small">
          每个因子 = 权重 × 计数，行内是它的首条依据（真实存在的观测 / 实体 /
          Inbox id）。分数只吃库里已有的事实 —— 零出站，不查 CVE 库。
        </p>
        <p v-if="!hitFactors.length" class="dim small">
          全部因子 0 命中 —— 库里还没有可计分的事实。
        </p>
        <table v-else class="factors">
          <tbody>
            <tr v-for="f in hitFactors" :key="f.id">
              <td class="mono id">{{ f.id }}</td>
              <td class="bar-cell">
                <div class="bar" :style="{ width: `${(f.contribution / maxContribution) * 100}%` }" />
              </td>
              <td class="num mono">{{ f.contribution }}</td>
              <td class="mono faint">{{ f.count }} × {{ f.weight }}</td>
              <td class="evidence faint">{{ evidenceText(f) }}</td>
            </tr>
          </tbody>
        </table>
        <!--
          摘要全部是后端给的分布（facts.*），前端不再自己算一遍 —— 这一页连
          "未处置占比"都不许推导：评分口径在 core/risk 一处，这里只负责摆。
        -->
        <dl v-if="facts" class="kv facts">
          <dt>漏洞</dt>
          <dd>{{ facts.vuln_total }}（{{ stateText }}）</dd>
          <dt>未决 Inbox</dt><dd>{{ facts.inbox_open }}</dd>
          <dt>未测覆盖</dt><dd>{{ facts.untested }}</dd>
          <dt>越界资产</dt><dd>{{ facts.out_of_scope }}</dd>
          <dt>敏感串</dt><dd>{{ facts.secrets }}</dd>
        </dl>
      </section>

      <section class="panel card wide">
        <h2 class="section">攻击面（按主机，共 {{ surface.totals.hosts }} 台）</h2>
        <p class="faint small">
          与评分共用同一份因子数据 —— 两个数字不可能各说各话。
          漏洞只计未处置（state=open）的。
        </p>
        <p v-if="!surface.hosts.length" class="dim small">
          没有暴露的 service / endpoint，也没有被打到的主机。
        </p>
        <table v-else class="hosts">
          <thead>
            <tr><th>HOST</th><th>服务（在跑的产品）</th><th>接口</th><th>未处置漏洞</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in surface.hosts" :key="row.host">
              <td class="mono">{{ row.host }}</td>
              <td>
                <span v-if="row.services.length">{{ hostServices(row) }}</span>
                <span v-else class="faint">—</span>
              </td>
              <td class="num mono">{{ row.endpoint_count }}</td>
              <td class="num mono" :class="row.open_vuln_total ? 'bad' : 'ok'">
                {{ row.open_vuln_total }}
              </td>
            </tr>
          </tbody>
        </table>
        <p v-if="unmapped.length" class="bad small">
          ⚠ {{ unmapped.length }} 条未处置漏洞没有 found_on 落点，不在任何主机行里
          （{{ unmapped.map((v) => v.key).join('、') }}）—— 攻击面比实际要少这几条。
        </p>
      </section>
    </div>
  </div>
</template>

<style scoped>
.risk { display: flex; flex-direction: column; height: 100%; padding: 12px; gap: 8px;
        overflow-y: auto; }
.head { display: flex; align-items: center; gap: 8px; }
.spacer { flex: 1; }
.total { font-size: 28px; font-weight: 700; font-variant-numeric: tabular-nums; }
.cols { display: grid; grid-template-columns: 1fr; gap: 12px; }
.card { padding: 12px; }
.wide { grid-column: 1 / -1; }
.factors { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 12px; }
.factors td { padding: 3px 8px 3px 0; vertical-align: top; }
.factors .id { white-space: nowrap; font-weight: 600; }
.bar-cell { width: 34%; padding-right: 10px !important; }
.bar { height: 10px; background: var(--accent, #6ea8fe); border-radius: 3px;
       min-width: 2px; margin-top: 3px; }
.factors .num { text-align: right; font-weight: 700;
                font-variant-numeric: tabular-nums; white-space: nowrap; }
.evidence { max-width: 340px; word-break: break-all; }
.hosts { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 12px; }
.hosts th { text-align: left; color: var(--faint); font-weight: 500;
            padding: 2px 8px 2px 0; }
.hosts td { padding: 3px 8px 3px 0; vertical-align: top; }
.hosts .num { text-align: right; font-variant-numeric: tabular-nums; }
.facts { margin-top: 10px; grid-template-columns: 92px 1fr; }
.kv { display: grid; gap: 3px 8px; margin: 0; font-size: 12px; }
.kv dt { color: var(--faint); }
.kv dd { margin: 0; }
.small { font-size: 11px; }
</style>
