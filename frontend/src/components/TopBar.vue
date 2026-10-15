<script setup>
import { computed } from 'vue'
import { store } from '../store.js'
import { fmtInt } from '../format.js'

defineProps({ version: { type: String, default: '' } })
const emit = defineEmits(['import', 'palette', 'manage'])
const label = computed(() => store.VIEWS.find(v => v.key === store.view)?.label || store.lensRows.find(v => v.key === store.view)?.label || store.view)
</script>

<template>
  <header class="workspace-topbar">
    <div class="brand"><span class="brand-symbol">m<span>●</span></span><strong>Magpie</strong><small class="mono faint">{{ version }}</small></div>
    <div class="topbar-divider" />
    <label class="project"><span class="dim">项目</span><select :value="store.project" aria-label="当前项目" @change="store.selectProject($event.target.value)"><option v-if="!store.projects.length" value="">（还没有项目）</option><option v-for="p in store.projects" :key="p.id" :value="p.name">{{ p.name }}（{{ fmtInt(p.counts.entities) }} 实体）</option></select></label>
    <button class="ghost" title="项目管理：新建 / 归档 / 恢复 / 删除" @click="emit('manage')">管理</button>
    <div class="topbar-location"><span class="faint">/</span> {{ label }}</div>
    <span class="topbar-spacer" />
    <button class="ghost topbar-search" title="搜索（Ctrl+K）" @click="emit('palette')">⌕ <span>搜索</span><kbd>Ctrl K</kbd></button>
    <span v-if="store.view === 'pipeline'" class="chip accent">✎ 编排</span><span v-else class="chip" title="探索模式只读画布；笔记与导入通过各自入口操作">🔒 只读浏览</span>
    <button class="primary" @click="emit('import')">＋ 导入</button>
  </header>
</template>
