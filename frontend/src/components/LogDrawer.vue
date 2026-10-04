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

//: 流式坏行明细（0.18.0）：`badLinesDetail` 是事件流推来的**部分**明细
//: （每批最多 50 条 + 明细表 1000 条上限），`badLines` 才是全数计数 ——
//: 两者不等时必须说出来，否则明细列表看起来就是"全部坏行"。
const badDetail = computed(() => store.importProgress.badLinesDetail)
const badlinesFiles = computed(() => {
  const report = store.importReport && store.importReport.report
  return (report && report.files || [])
    .filter((file) => file.badlines_report)
    .map((file) => file.badlines_report)
})

function stamp(at) {
  const date = new Date(at)
  return `${String(date.getHours()).padStart(2, '0')}:` +
    `${String(date.getMinutes()).padStart(2, '0')}:` +
    `${String(date.getSeconds()).padStart(2, '0')}`
}

// ---------------------------------------------------------------------------
// 拖高（视觉整改）。96 再矮只剩页签、520 再高把画布整个吃掉 —— clamp 写死
// 在这里而不是 store，因为这是**这一个组件**的尺寸约束，不是共享状态的语义。
// ---------------------------------------------------------------------------
const H_MIN = 96
const H_MAX = 520

/**
 * 把手按下 → 捕获指针后跟随移动。`setPointerCapture` 是关键：光标拖出抽屉
 * 范围（或快到屏幕边缘）时事件仍送回把手 —— 没有它，拖到一半鼠标一快就"断线"。
 */
function onGripDown(event) {
  const target = event.currentTarget
  const startY = event.clientY
  const startHeight = store.drawerHeight
  target.setPointerCapture?.(event.pointerId)
  const onMove = (moveEvent) => {
    // 向上拖 = 变高（clientY 变小），从按下的那一刻的高度起算
    const next = startHeight + (startY - moveEvent.clientY)
    store.drawerHeight = Math.min(H_MAX, Math.max(H_MIN, next))
  }
  const onUp = (upEvent) => {
    target.releasePointerCapture?.(upEvent.pointerId)
    target.removeEventListener('pointermove', onMove)
    target.removeEventListener('pointerup', onUp)
    target.removeEventListener('pointercancel', onUp)
  }
  target.addEventListener('pointermove', onMove)
  target.addEventListener('pointerup', onUp)
  target.addEventListener('pointercancel', onUp)
}

//: 双击页签栏折叠/展开（快捷方式；按钮还在，手势是加的不是换的）。
//: 落在按钮上的双击不算 —— 那是在连点两个按钮，不是在拍页签栏。
function onTabsDblClick(event) {
  if (event.target.closest('button')) return
  store.drawerOpen = !store.drawerOpen
}
</script>

<template>
  <section class="drawer" :class="{ collapsed: !store.drawerOpen }">
    <!-- 拖高把手：贴在抽屉最上沿的窄条（双击页签栏 = 折叠，把手本身只管拖）。 -->
    <div class="grip" title="拖动调高" @pointerdown="onGripDown" />
    <header class="tabs" @dblclick="onTabsDblClick">
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

    <div v-show="store.drawerOpen" class="body"
         :style="{ height: store.drawerHeight + 'px' }">
      <template v-if="tab === 'import'">
        <div v-if="store.importProgress.state" class="progress">
          阶段 {{ store.importProgress.stage || '—' }}
          <template v-if="store.importProgress.fileTotal">
            · 文件 {{ store.importProgress.fileIndex }}/{{ store.importProgress.fileTotal }}
          </template>
          · 累计 {{ store.importProgress.records }} 条记录 /
          本文件 {{ store.importProgress.line }} 行 ·
          坏行 {{ store.importProgress.badLines }}
          <span v-if="store.importProgress.files.length">
            · 已完成 {{ store.importProgress.files.length }} 个文件</span>
        </div>
        <div v-for="(line, i) in importLines" :key="i" class="line"
             :class="LEVEL_CLASS[line.level]">
          <span class="at faint">{{ stamp(line.at) }}</span>{{ line.text }}
        </div>
        <p v-if="!importLines.length" class="dim pad">
          还没有导入。顶部「导入」把 subfinder / httpx 的输出收进来，进度在这里实时出现。
        </p>
      </template>

      <template v-else-if="tab === 'activity'">
        <div v-for="(line, i) in activityLines" :key="i" class="line"
             :class="LEVEL_CLASS[line.level]">
          <span class="at faint">{{ stamp(line.at) }}</span>{{ line.text }}
        </div>
        <p v-if="!activityLines.length" class="dim pad">
          还没有记录。点画布上的一条连线，它的关系与置信度会在这里留一行。
        </p>
      </template>

      <!-- 坏行明细（0.18.0 起真渲染）：流式明细 + 计数 + 全量落盘路径。 -->
      <template v-else-if="tab === 'badlines'">
        <div v-for="(bad, i) in badDetail" :key="i" class="line">
          <span class="at faint">{{ bad.file }}:{{ bad.line }}</span>
          {{ bad.reason }}
          <span v-if="bad.raw" class="faint"> — {{ bad.raw }}</span>
        </div>
        <p v-if="badDetail.length && store.importProgress.badLines > badDetail.length"
           class="dim pad">
          明细是流式推送的一部分（{{ badDetail.length }} /
          {{ store.importProgress.badLines }} 条）—— 全量与行原文在数据区落盘，
          路径在下面。
        </p>
        <ul v-if="badlinesFiles.length" class="files">
          <li v-for="(path, i) in badlinesFiles" :key="'f' + i" class="mono">
            {{ path }}
          </li>
        </ul>
        <p v-if="!badDetail.length" class="dim pad">
          还没有坏行。解析器把格式漂移的原文行拦在这里 —— 导入时会逐条实时列出，
          全量另存在数据区的 reports/&lt;job&gt;.badlines.txt。
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
/* 把手：平时只有一道居中的短横（收拾得像一条缝），hover 变色提示可拖。 */
.grip {
  height: 6px;
  cursor: ns-resize;
  position: relative;
  touch-action: none;        /* 触屏上拖动不许变成页面滚动 */
}
.grip::after {
  content: '';
  position: absolute;
  top: 1px;
  left: 50%;
  transform: translateX(-50%);
  width: 56px;
  height: 3px;
  border-radius: 2px;
  background: var(--line);
}
.grip:hover::after { background: var(--accent); }
.tabs { display: flex; align-items: center; gap: 4px; padding: 5px 10px; }
.tab { border-color: transparent; color: var(--dim); display: inline-flex; gap: 5px; }
.tab.active { border-color: var(--accent); color: var(--accent); }
.spacer { flex: 1; }
.body {
  /* 高度走 store.drawerHeight（拖动把手改它）；overflow 让长日志滚动。 */
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
.files { list-style: none; margin: 4px 0 0; padding: 0; font-size: 10px;
         word-break: break-all; color: var(--faint); }
.files li { padding: 1px 0; }
</style>
