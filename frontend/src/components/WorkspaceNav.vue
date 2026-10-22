<script setup>
import { computed } from 'vue'
import { store } from '../store.js'

const research = [
  ['overview', '◈', '总览', 'Overview'], ['assets', '▦', '资产', 'Assets'],
  ['services', '⊞', '服务与中间件', 'Services'], ['api', '↗', 'API 接口', 'API'],
  ['js', '⌘', 'JS 资源', 'JavaScript'], ['findings', '◇', '漏洞与发现', 'Findings'],
  ['evidence', '▤', '证据', 'Evidence'], ['jobs', '↓', '导入历史', 'Imports'],
  ['notes', '≡', '研究笔记', 'Notes'],
  ['tasks', '☑', '研究任务', 'Tasks'],
  ['changes', 'Δ', '增量变化', 'Changes'],
]
const advanced = computed(() => {
  const base = [['explore', '攻击面画布'], ['pipeline', '管线编排'], ['inbox', '待确认 Inbox'],
    ['risk', '风险评估'], ['doctor', '数据体检'], ['mcp', 'MCP 接入']]
  for (const row of store.lensRows || []) {
    if (![...research.map(r => r[0]), ...base.map(r => r[0])].includes(row.key)) base.push([row.key, row.label])
  }
  return base
})
</script>

<template>
  <aside class="left workspace-nav" aria-label="项目导航">
    <div class="nav-heading"><span class="project-mark">M</span><div><strong>研究工作台</strong><small>LOCAL WORKSPACE</small></div></div>
    <div class="nav-caption">项目研究</div>
    <nav>
      <button v-for="[key, icon, label, en] in research" :key="key" :class="{ current: store.view === key }"
              :aria-current="store.view === key ? 'page' : undefined" @click="store.setView(key)">
        <span class="nav-icon" aria-hidden="true">{{ icon }}</span><span>{{ label }}<small>{{ en }}</small></span>
        <span v-if="key === 'jobs'" class="nav-arrow">↗</span>
      </button>
    </nav>
    <div class="nav-caption advanced-caption">工具与治理</div>
    <nav class="advanced-nav"><button v-for="[key, label] in advanced" :key="key" :class="{ current: store.view === key }"
      @click="store.setView(key)">{{ label }}<span v-if="key === 'inbox' && store.openInboxCount" class="badge">{{ store.openInboxCount }}</span></button></nav>
    <div class="nav-footer"><span class="local-dot" />本地数据 · 零主动扫描<small>导入、研究、回查</small></div>
  </aside>
</template>
