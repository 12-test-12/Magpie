<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { store } from '../store.js'
import { fmtInt } from '../format.js'
import { params, requestSlot } from '../workspace.js'
import EmptyGuide from '../components/EmptyGuide.vue'

const emit = defineEmits(['import'])
const data = ref(null), loading = ref(false), error = ref('')
const guide = computed(() => !store.projects.length || !(store.projectTotal > 0 || store.counts.entities > 0))
const slot = requestSlot()
const cards = [['assets','资产','域名 / IP / 主机 / 网段','▦'], ['services','服务','服务与产品观测','⊞'],
  ['api','API 接口','规范键与 HTTP 观测','↗'], ['evidence','来源证据','原始定位与归档','▤']]
function load() {
  slot.cancel(); data.value = null; error.value = ''
  if (!store.project || guide.value) return
  loading.value = true
  slot.run('/workspace/summary?' + params({ project: store.project }), result => { data.value = result },
    text => { error.value = text }, () => { loading.value = false })
}
watch(() => [store.project, guide.value, store.dataRevision], load, { immediate: true })
onBeforeUnmount(() => slot.cancel())
</script>

<template>
  <EmptyGuide v-if="guide" @import="emit('import')" />
  <div v-else class="research-page overview-page">
    <header class="page-heading"><div><div class="eyebrow">RESEARCH WORKSPACE / OVERVIEW</div><h1>攻击面安全总览</h1><p>从已导入的数据出发，找到下一步值得研究的对象。</p></div><button class="primary" @click="emit('import')">＋ 导入数据</button></header>
    <div class="overview-hero"><div><span class="hero-tag"><span class="local-dot" /> LOCAL RESEARCH</span><h2>让线索有来源，<br />让研究有路径。</h2><p>按主机聚合资产，沿接口与证据下钻。<br />每一条结论，都可以回到原始数据。</p><button class="primary" @click="store.setView('assets')">打开资产工作区 ↗</button><button class="ghost" @click="store.setView('explore')">进入攻击面画布 →</button></div><div class="hero-map" aria-hidden="true"><span class="map-node root">WORKSPACE</span><i /><div><span class="map-node">ASSETS</span><span class="map-node highlight">API</span><span class="map-node">EVIDENCE</span></div><small>导入 → 聚合 → 下钻 → 回查</small></div></div>
    <div v-if="loading" class="workspace-state" role="status">正在读取项目摘要…</div>
    <div v-else-if="error" role="alert" class="workspace-state error-state"><strong>摘要暂不可用</strong><p>{{ error }}</p><button @click="load">重试</button></div>
    <template v-else-if="data">
      <div class="overview-cards"><button v-for="[key,label,hint,icon] in cards" :key="key" class="metric-card" @click="store.setView(key)"><span>{{ label }} <span class="metric-icon">{{ icon }}</span></span><strong>{{ fmtInt(data[key]) }}</strong><small>{{ hint }} <span>↗</span></small></button></div>
      <div class="overview-columns"><section class="overview-section"><header><h2>待关注事项</h2><span class="dim">可解释的研究入口</span></header><button class="attention-row" @click="store.setView('inbox')"><span class="attention-icon">?</span><span><strong>待确认的导入与冲突</strong><small>查看 Inbox，检查仍需人工裁决的信息</small></span><b>{{ fmtInt(data.pending) }}</b><span>→</span></button><button class="attention-row" @click="store.setView('vuln')"><span class="attention-icon warning">◇</span><span><strong>工具发现 · 待核实</strong><small>扫描器命中不等同于已确认漏洞</small></span><b>{{ fmtInt(data.findings) }}</b><span>→</span></button><button class="attention-row" @click="store.setView('jobs')"><span class="attention-icon warning">!</span><span><strong>执行失败的作业</strong><small>部分导入与覆盖缺口请逐项查看导入报告</small></span><b>{{ fmtInt(data.failed_jobs) }}</b><span>→</span></button><p class="count-basis">{{ data.count_basis }}。未提供时间差分，不将总量标作“新增”。</p></section>
        <section class="overview-section"><header><h2>最近导入与操作</h2><button class="ghost" @click="store.setView('jobs')">全部历史 ↗</button></header><p v-if="!data.recent_jobs.length" class="workspace-state">还没有作业记录</p><button v-for="job in data.recent_jobs" :key="job.id" class="recent-job" @click="store.setView('jobs')"><span class="job-icon">↓</span><span><strong>{{ job.kind }} <small>#{{ job.id }}</small></strong><small>{{ job.started_at }}</small></span><span class="job-state">{{ job.state }}{{ job.applied ? '' : ' · 未应用' }}</span></button></section></div>
    </template>
    <footer class="overview-footer"><span>MAGPIE / 证据驱动的本地研究工作台</span><button class="ghost" @click="load">刷新摘要 ↻</button></footer>
  </div>
</template>
