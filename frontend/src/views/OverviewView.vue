<script setup>
/**
 * 项目总览 —— 默认首页（GPT 评审十一）。
 *
 * 安全产品不该一进来就是一张图：打开项目第一眼要回答的是
 * **「这个项目现在怎么样」**，而不是"这些资产怎么连起来"（那是攻击面画布的
 * 问题，从这里的一次显式动作进去）。三个区块：
 *
 *   资产四象限  —— 域名 / IP / 服务 / 接口四个大数 + 其余类型补充条。
 *                 数据源 `store.lensRows`（`GET /lenses`，**库里的**数），
 *                 与左栏「资产规模」同源 —— 两个地方各数一遍迟早分叉。
 *   风险概览    —— `GET /risk`（风险页同一份口径，界面不自算）：总分、
 *                 摘要事实、命中因子 top3。
 *   重点风险资产 —— `/risk` 的 `attack_surface.hosts` 按未处置漏洞降序 top6。
 *
 * **刻意不做「过去 7 天攻击面变化」**（评审里的趋势区块）：后端没有按时间
 * 比较资产数的 API，前端拿 `first_seen` 自己数会造出第二份口径 —— 而两个
 * 口径不会同时出错、也不会被人一眼看穿。等有只读的趋势端点再加。
 *
 * 空库时整页渲染 `EmptyGuide`（评审十一的引导卡与 app.test 钉死的
 * 「从这里开始 / 还没有数据 / 开始导入」全在那里面）—— 引导卡从此长在首页上，
 * 而不是藏在一个要先点进去的画布里。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { get } from '../api.js'
import { store } from '../store.js'
import { fmtInt } from '../format.js'
import EmptyGuide from '../components/EmptyGuide.vue'

const emit = defineEmits(['import'])

//: 与 EmptyGuide 内部同一条判据（项目数 / 项目实体数）—— 这里只决定"整页
//: 是不是引导卡"，三分支的文案与动作全在 EmptyGuide 里，不复制第二份。
const guide = computed(() => !store.projects.length
  || !(store.projectTotal > 0 || store.counts.entities > 0))

// ---------------------------------------------------------------------------
// 资产四象限
// ---------------------------------------------------------------------------

//: 四象限按评审的口径固定为这四类（标签中文 + kind 原文都给 —— 安全同学两种
//: 词汇都在用）。库里没有的类型显示 0，**不隐藏**：0 也是一个回答。
const QUADS = [
  { kind: 'domain', label: '域名' },
  { kind: 'ip', label: 'IP' },
  { kind: 'service', label: '服务' },
  { kind: 'endpoint', label: '接口' },
]

//: 跨镜头摊平（与左栏/详情面板默认态同一段算法 —— lenses.kinds 是唯一数据源）。
const kindSummary = computed(() => {
  const rows = []
  for (const row of store.lensRows) {
    for (const kind of (row.data && row.data.kinds) || []) {
      rows.push({ kind: kind.kind, count: Number(kind.count) || 0 })
    }
  }
  return rows.sort((a, b) => b.count - a.count || a.kind.localeCompare(b.kind))
})

const quadRows = computed(() => QUADS.map((quad) => ({
  ...quad,
  count: (kindSummary.value.find((row) => row.kind === quad.kind) || {}).count || 0,
})))

//: 其余类型（四象限之外的一切）：cidr / host / product / secret / vuln / flow…
//: 按数量降序补一条 —— secret 13 个不会因为它不在四个大类里就消失。
const restRows = computed(() => kindSummary.value
  .filter((row) => !QUADS.some((quad) => quad.kind === row.kind)))

// ---------------------------------------------------------------------------
// 风险概览 + 重点风险资产（GET /risk，与风险页同一份口径）
// ---------------------------------------------------------------------------

const risk = ref(null)
const riskLoading = ref(false)
const riskError = ref('')
//: 按项目记账：换项目 = 换了一个库的事实，必须重拉（风险页同款纪律）。
const riskProject = ref(null)

async function loadRisk(project) {
  riskLoading.value = true
  riskError.value = ''
  try {
    const query = project ? `?project=${encodeURIComponent(project)}` : ''
    risk.value = await get(`/risk${query}`)
    riskProject.value = project
  } catch (err) {
    risk.value = null
    riskError.value = err.message
  } finally {
    riskLoading.value = false
  }
}

onMounted(() => {
  if (!guide.value && store.project && riskProject.value !== store.project) {
    loadRisk(store.project)
  }
})
watch(() => store.project, (project) => {
  if (!guide.value && project && riskProject.value !== project) loadRisk(project)
})
watch(() => store.view, (value) => {
  if (value === 'overview' && !guide.value && store.project
      && riskProject.value !== store.project) {
    loadRisk(store.project)
  }
})
//: 引导卡 → 有数据（导入完成）：总览页还挂着的时候立刻补一次风险拉取。
watch(guide, (nowGuide) => {
  if (!nowGuide && store.project && riskProject.value !== store.project) {
    loadRisk(store.project)
  }
})

const score = computed(() => risk.value && risk.value.score)
const facts = computed(() => risk.value && risk.value.facts)
const surface = computed(() => risk.value && risk.value.attack_surface)

const hitFactors = computed(() => {
  if (!score.value || !Array.isArray(score.value.factors)) return []
  return score.value.factors.filter((f) => f.count > 0)
    .slice()
    .sort((a, b) => b.contribution - a.contribution)
    .slice(0, 3)
})

//: 漏洞状态分布：后端给的直排，这里只拼文字（口径在 core/risk 一处）。
const stateText = computed(() => {
  if (!facts.value) return ''
  const entries = Object.entries(facts.value.vuln_by_state || {})
  return entries.length ? entries.map(([k, v]) => `${k} ${v}`).join('、') : '无'
})

//: 重点风险资产：按未处置漏洞降序 top6。0 漏洞的主机不进榜 —— "重点"的
//: 定义就是有未处置命中的那些，把 0 们铺进来等于没有排序。
const keyRiskHosts = computed(() => {
  if (!surface.value || !Array.isArray(surface.value.hosts)) return []
  return surface.value.hosts
    .filter((row) => Number(row.open_vuln_total) > 0)
    .slice()
    .sort((a, b) => Number(b.open_vuln_total) - Number(a.open_vuln_total))
    .slice(0, 6)
})

const unmapped = computed(() => (surface.value && surface.value.totals
  ? surface.value.totals.unmapped_open_vulns || [] : []))

function hostServices(row) {
  return row.services.map((s) => (s.products && s.products.length
    ? `${s.key}（${s.products.join('、')}）` : s.key)).join('、')
}
</script>

<template>
  <div class="page">
    <!-- 空库引导：文案与按钮全在 EmptyGuide（钉死的三条空状态）。 -->
    <template v-if="guide">
      <EmptyGuide @import="emit('import')" />
    </template>

    <template v-else>
      <header class="head">
        <div class="title">
          <h2 class="section">攻击面安全总览</h2>
          <span class="faint small">{{ store.project || '（未选项目）' }}</span>
        </div>
        <span class="total">
          <b>{{ fmtInt(store.projectTotal) }}</b>
          <span class="faint">实体（库中）</span>
        </span>
        <span class="spacer" />
        <button class="primary" @click="store.setView('explore')">
          进入攻击面画布 →
        </button>
      </header>

      <!-- 资产四象限 -->
      <section class="panel card">
        <h2 class="section">资产</h2>
        <div class="quads">
          <div v-for="row in quadRows" :key="row.kind" class="quad">
            <span class="qlabel">{{ row.label }}</span>
            <b class="mono">{{ fmtInt(row.count) }}</b>
            <span class="qkind mono faint">{{ row.kind }}</span>
          </div>
        </div>
        <ul v-if="restRows.length" class="rest">
          <li v-for="row in restRows" :key="row.kind">
            <span class="mono">{{ row.kind }}</span>
            <span class="num">{{ fmtInt(row.count) }}</span>
          </li>
        </ul>
        <p v-if="!kindSummary.length" class="dim small">
          （镜头计数载入中…）
        </p>
      </section>

      <!-- 风险概览 -->
      <section class="panel card">
        <h2 class="section">风险概览</h2>
        <p v-if="riskError" class="bad small">风险分拉取失败：{{ riskError }}</p>
        <p v-else-if="riskLoading && !risk" class="dim small">评分中…</p>
        <template v-if="score">
          <div class="score-row">
            <b class="score">{{ score.total }}</b>
            <span class="faint">风险分</span>
            <span v-if="score.total === 0" class="chip ok">
              零命中 —— 这是结果，不是错误
            </span>
            <span class="chip" :title="risk.rules_digest">
              规则 v{{ risk.rule_version }}
            </span>
          </div>
          <dl v-if="facts" class="kv">
            <dt>漏洞</dt><dd>{{ facts.vuln_total }}（{{ stateText }}）</dd>
            <dt>未决 Inbox</dt><dd>{{ facts.inbox_open }}</dd>
            <dt>未测覆盖</dt><dd>{{ facts.untested }}</dd>
            <dt>越界资产</dt><dd>{{ facts.out_of_scope }}</dd>
            <dt>敏感串</dt><dd>{{ facts.secrets }}</dd>
          </dl>
          <p v-if="hitFactors.length" class="faint small">
            命中因子 top3：
            <span v-for="f in hitFactors" :key="f.id" class="factor mono">
              {{ f.id }} +{{ f.contribution }}
            </span>
            —— 完整分解在「风险」页。
          </p>
        </template>
        <p v-else-if="risk && !score" class="dim small">（返回里没有 score）</p>
      </section>

      <!-- 重点风险资产 -->
      <section class="panel card">
        <h2 class="section">重点风险资产</h2>
        <p class="faint small">
          按主机的未处置漏洞（state=open）降序 —— 与「风险」页的攻击面表同一份
          数据（`GET /risk`）。分数只吃库里已有的事实，零出站。
        </p>
        <p v-if="surface && !keyRiskHosts.length" class="dim small">
          没有未处置漏洞命中的主机 —— 这是结果，不是空数据。
        </p>
        <table v-else-if="keyRiskHosts.length" class="hosts">
          <thead>
            <tr><th>HOST</th><th>服务</th><th>接口</th><th>未处置漏洞</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in keyRiskHosts" :key="row.host">
              <td class="mono">{{ row.host }}</td>
              <td>
                <span v-if="row.services.length">{{ hostServices(row) }}</span>
                <span v-else class="faint">—</span>
              </td>
              <td class="num mono">{{ row.endpoint_count }}</td>
              <td class="num mono bad">{{ row.open_vuln_total }}</td>
            </tr>
          </tbody>
        </table>
        <p v-if="unmapped.length" class="bad small">
          ⚠ {{ unmapped.length }} 条未处置漏洞没有 found_on 落点，不在任何主机行里
          —— 攻击面比实际要少这几条。
        </p>
      </section>
    </template>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 12px;
  height: 100%;
  padding: 12px;
  overflow-y: auto;
}
.head { display: flex; align-items: center; gap: 14px; }
.title { display: flex; align-items: baseline; gap: 10px; }
.title h2 { margin: 0; }
.total { display: flex; align-items: baseline; gap: 6px; }
.total b {
  font-size: 24px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  line-height: 1;
}
.total .faint { font-size: 11px; }
.spacer { flex: 1; }
.card { padding: 12px; }
.small { font-size: 11px; }

/* 四象限：四张等宽大数卡，一眼读出资产结构。 */
.quads {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
  margin-top: 8px;
}
.quad {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius-card);
  background: var(--panel-2);
}
.qlabel { font-size: 11px; color: var(--dim); }
.quad b {
  font-size: 22px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  line-height: 1.1;
}
.qkind { font-size: 10px; }

/* 其余类型补充条：secret / vuln 这些不在四个大类里的数字不许消失。 */
.rest {
  list-style: none;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
  margin: 10px 0 0;
  padding: 8px 0 0;
  border-top: 1px solid var(--line-soft);
  font-size: 11px;
}
.rest li { display: flex; gap: 6px; align-items: baseline; }
.rest .num { color: var(--dim); font-variant-numeric: tabular-nums; }

.score-row { display: flex; align-items: baseline; gap: 8px; margin-top: 6px; }
.score {
  font-size: 26px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  line-height: 1;
}
.kv {
  display: grid;
  grid-template-columns: 84px 1fr;
  gap: 3px 8px;
  margin: 10px 0 0;
  font-size: 12px;
}
.kv dt { color: var(--faint); }
.kv dd { margin: 0; }
.factor { margin-right: 10px; color: var(--dim); }

.hosts { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 12px; }
.hosts th { text-align: left; color: var(--faint); font-weight: 500; padding: 2px 8px 2px 0; }
.hosts td { padding: 3px 8px 3px 0; vertical-align: top; }
.hosts .num { text-align: right; font-variant-numeric: tabular-nums; }
</style>
