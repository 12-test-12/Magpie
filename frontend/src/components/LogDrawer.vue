<script setup>
/**
 * 底部日志抽屉（计划书 §7.1 的"执行日志"）。
 *
 * 三个页签对应三件**不同**的事，不要混成一条时间线：
 *
 *   导入作业  —— 这一次导入的进度 / 阶段 / 坏行（SSE 推来的，实时）
 *   agent 活动 —— `activity_log` 里"谁在什么时候动了什么"（拉来的，有落库）
 *   坏行明细  —— 格式漂移的原文行（§5.6：失败行必须可见）
 *
 * 把它们合成一条流会让人分不清"这是刚才那次导入的进度"还是"上周某次作业的
 * 记录" —— 而这两件事的处理方式完全不同。
 */
import { computed, ref } from 'vue'
import { store } from '../store.js'

const tab = ref('import')

const importLines = computed(() =>
  store.logLines.filter((line) => line.kind === 'import'))
const activityLines = computed(() =>
  store.logLines.filter((line) => line.kind === 'activity'))

const LEVEL_CLASS = { error: 'bad', warn: 'warn', info: '' }

function stamp(at) {
  const date = new Date(at)
  return `${String(date.getHours()).padStart(2, '0')}:` +
    `${String(date.getMinutes()).padStart(2, '0')}:` +
    `${String(date.getSeconds()).padStart(2, '0')}`
}
</script>

<template>
  <section class="drawer" :class="{ collapsed: !store.drawerOpen }">
    <header class="tabs">
      <button v-for="item in [
          { key: 'import', label: '导入作业' },
          { key: 'activity', label: 'agent 活动' },
          { key: 'badlines', label: '坏行明细' },
        ]"
        :key="item.key"
        class="ghost tab"
        :class="{ active: tab === item.key }"
        @click="tab = item.key; store.drawerOpen = true"
      >
        {{ item.label }}
        <span v-if="item.key === 'import' && importLines.length" class="badge">
          {{ importLines.length }}</span>
      </button>
      <span class="spacer" />
      <span v-if="store.activeImportId" class="chip mono">
        {{ store.activeImportId }} · {{ store.importProgress.state || '…' }}
      </span>
      <span v-if="store.importProgress.dropped" class="chip warn"
            title="慢消费者丢帧是允许的，但丢帧不静默">
        丢帧 {{ store.importProgress.dropped }}</span>
      <button class="ghost" @click="store.clearLog()">清空</button>
      <button class="ghost" @click="store.drawerOpen = !store.drawerOpen">
        {{ store.drawerOpen ? '收起' : '展开' }}
      </button>
    </header>

    <div v-show="store.drawerOpen" class="body">
      <template v-if="tab === 'import'">
        <div v-if="store.importProgress.state" class="progress">
          阶段 {{ store.importProgress.stage || '—' }} ·
          {{ store.importProgress.records }} 条记录 /
          {{ store.importProgress.line }} 行 ·
          坏行 {{ store.importProgress.badLines }}
        </div>
        <div v-for="(line, i) in importLines" :key="i" class="line"
             :class="LEVEL_CLASS[line.level]">
          <span class="at faint">{{ stamp(line.at) }}</span>{{ line.text }}
        </div>
        <p v-if="!importLines.length" class="dim pad">
          还没有导入。顶部「导入」按钮把 subfinder / httpx 的输出收进来，
          进度会在这里实时出现（SSE，每 100ms 一批）。
        </p>
      </template>

      <template v-else-if="tab === 'activity'">
        <div v-for="(line, i) in activityLines" :key="i" class="line"
             :class="LEVEL_CLASS[line.level]">
          <span class="at faint">{{ stamp(line.at) }}</span>{{ line.text }}
        </div>
        <p v-if="!activityLines.length" class="dim pad">
          还没有记录。点画布上的一条连线，它的关系与置信度会在这里留一行 ——
          这只是本地的操作日志，落库的那份在「作业历史」页。
        </p>
      </template>

      <template v-else>
        <p class="dim pad">
          坏行明细来自导入报告（`report.files[].bad_lines`）。请到「作业历史」
          选一次导入的 job，在报告里看逐行明细 —— 那里是**落库**的那一份，
          抽屉里的这份只是本次会话的实时流，刷新就没了。
        </p>
      </template>
    </div>
  </section>
</template>

<style scoped>
.drawer {
  flex: none;
  border-top: 1px solid var(--line);
  background: var(--panel);
  display: flex;
  flex-direction: column;
}
.drawer.collapsed { height: auto; }
.tabs { display: flex; align-items: center; gap: 4px; padding: 5px 10px; }
.tab { border-color: transparent; color: var(--dim); display: inline-flex; gap: 5px; }
.tab.active { border-color: var(--accent); color: var(--accent); }
.spacer { flex: 1; }
.body {
  height: 190px;
  overflow-y: auto;
  padding: 4px 12px 10px;
  font-family: var(--font-mono);
  font-size: 11px;
}
.line { padding: 1px 0; word-break: break-all; }
.at { margin-right: 8px; }
.pad { padding: 6px 0; font-family: var(--font-ui); font-size: 12px; }
.progress {
  font-family: var(--font-ui);
  color: var(--dim);
  padding-bottom: 5px;
  border-bottom: 1px solid var(--line-soft);
  margin-bottom: 5px;
}
</style>
