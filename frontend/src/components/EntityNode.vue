<script setup>
/**
 * 实体卡片（计划书 §7.1 的"节点卡片"）。
 *
 *   图标 + 名称 + 副标题 + 状态点 + 徽标
 *
 * 尺寸**固定**（§7.3 防视觉混乱第 ① 条）：`NODE_WIDTH × NODE_HEIGHT` 是常量，
 * 与内容的长度无关。长 key 截断而不是换行 —— 一个会随内容变高的卡片，会让
 * 整个布局在第一次真实数据到来时全部作废。
 *
 * 这个组件**没有任何 Handle**。Vue Flow 的 `Handle` 是"从这里拉一条线出去"的
 * 入口，而探索模式里不存在这件事。不渲染它，连误操作的入口都没有。
 *
 * 卡片上唯一的按钮是**折叠**（3.8）。它是"改显示"而不是"改数据"：折叠过的
 * 卡片淡下去、连着它的线也淡下去（把噪音收起来），一个事实都不动。它从卡片根
 * 元素**冒泡一个 DOM 事件**而不是直接调 store —— 卡片不需要知道 store 长什么
 * 样，而那个事件由 `GraphCanvas` 程序化收下交给 `store.toggleCollapse`（顺带把
 * 坐标带上，见那边的注释）。
 *
 * 为什么不用 Vue 的 `emit`：Vue Flow 渲染自定义节点时只传固定的上下文 props，
 * **不透传组件上的监听器** —— `emit('toggle-collapse')` 发出去无人接收，按钮
 * 就是个摆设（这个 bug 存在了很久，因为开发环境数据少、很少点它）。
 */
import { computed } from 'vue'
import { NODE_HEIGHT, NODE_WIDTH, kindColor } from '../graph.js'

const props = defineProps({
  data: { type: Object, required: true },
  selected: { type: Boolean, default: false },
})

/** 折叠：从卡片根元素发一个冒泡的 CustomEvent，GraphCanvas 用 addEventListener 收。 */
function onFoldClick(event) {
  const root = event.currentTarget && event.currentTarget.closest
    ? event.currentTarget.closest('.card') : null
  if (!root) return
  root.dispatchEvent(new CustomEvent('toggle-collapse', { bubbles: true }))
}

const color = computed(() => kindColor(props.data.kind))

const LIFECYCLE_TONE = {
  active: 'ok',
  stale: 'warn',
  retired: 'faint',
}

const subtitle = computed(() => {
  if (props.data.aggregated) return `${props.data.count} 个实体`
  return props.data.kind
})
</script>

<template>
  <div
    class="card"
    :class="{ selected, folded: data.collapsed }"
    :style="{ width: NODE_WIDTH + 'px', height: NODE_HEIGHT + 'px', '--band': color }"
    :data-entity-id="data.entityId"
    :data-collapsed="data.collapsed ? '1' : '0'"
    :title="data.key"
  >
    <span class="band" />
    <div class="body">
      <div class="head">
        <span class="dot" :class="LIFECYCLE_TONE[data.lifecycle] || 'faint'" />
        <span class="key mono">{{ data.key || ('#' + data.entityId) }}</span>
        <button v-if="!data.aggregated" class="fold"
                :title="data.collapsed ? '展开' : '折叠（只是把它画轻一点）'"
                @click.stop="onFoldClick($event)">
          {{ data.collapsed ? '▸' : '▾' }}
        </button>
      </div>
      <div class="foot">
        <span class="kind" :style="{ color }">{{ subtitle }}</span>
        <template v-if="data.aggregated">
          <span class="badge">{{ data.count }}</span>
        </template>
        <template v-else>
          <span v-if="data.degree" class="badge" title="关系数">{{ data.degree }}</span>
          <span v-if="data.collapsed" class="chip" title="已折叠">折叠</span>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.card {
  position: relative;
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-radius: var(--radius-card);
  overflow: hidden;
  cursor: grab;
  user-select: none;
}
.card.selected { box-shadow: var(--shadow-select); border-color: var(--accent); }
.card.folded { opacity: .5; }
.fold {
  margin-left: auto; flex: none; border: 0; background: transparent;
  color: var(--faint); cursor: pointer; font-size: 10px; padding: 0 2px;
}
.fold:hover { color: var(--text, #e6e6ea); }
.band {
  position: absolute;
  inset: 0 0 auto 0;
  height: 3px;
  background: var(--band);
}
.body {
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  height: 100%;
  padding: 11px 11px 8px;
}
.head { display: flex; align-items: center; gap: 6px; min-width: 0; }
.dot { width: 6px; height: 6px; border-radius: 50%; background: var(--faint); flex: none; }
.dot.ok { background: var(--ok); }
.dot.warn { background: var(--warn); }
.key {
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.foot { display: flex; align-items: center; gap: 6px; }
.kind { font-size: 11px; letter-spacing: .02em; }
</style>
