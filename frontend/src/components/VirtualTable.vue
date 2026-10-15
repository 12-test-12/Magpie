<script setup>
/**
 * 虚拟滚动的表格（计划书 §7.5：表格虚拟滚动，页大小 200）。
 *
 * **为什么不用现成的表格组件**：这个项目里表格只有一种形态 —— 定高行、左对齐、
 * 无编辑。为它引一个依赖，换来的是几百 KB 的运行时和一整套与本项目无关的
 * 交互约定（排序、列宽拖拽、单元格编辑），而其中一半在只读页面上是**不该有**的
 * （能编辑的表格会让人以为数据可以在这里改）。
 *
 * 窗口化的口径：只渲染可视区 ± buffer 行。10 万行的资产表在 DOM 里同样只有
 * 几十个 `tr` —— 这是"表格虚拟滚动"这条交付标准的全部内容。
 *
 * 排序与过滤**不在这里做**：那属于 SQL（§7.5 原话）。这个组件只负责"把已经
 * 排好序的行渲染得便宜"。在 JS 里排 10 万行是能跑，但它和数据库里 `ORDER BY`
 * 走索引不是一回事，而且会把"分页在 SQL"这条纪律悄悄挪到前端。
 */
import { computed, ref } from 'vue'

const props = defineProps({
  rows: { type: Array, default: () => [] },
  columns: { type: Array, required: true },   // [{key, label, mono, width}]
  rowHeight: { type: Number, default: 28 },
  buffer: { type: Number, default: 8 },
  selectedKey: { type: [String, Number], default: null },
  emptyText: { type: String, default: '（没有数据）' },
})

const emit = defineEmits(['row-click'])

const scroller = ref(null)
const scrollTop = ref(0)
const viewportHeight = ref(320)

const visibleCount = computed(() =>
  Math.ceil(viewportHeight.value / props.rowHeight) + props.buffer * 2)

const start = computed(() => Math.max(0,
  Math.floor(scrollTop.value / props.rowHeight) - props.buffer))

const windowRows = computed(() =>
  props.rows.slice(start.value, start.value + visibleCount.value))

const totalHeight = computed(() => props.rows.length * props.rowHeight)
const offsetY = computed(() => start.value * props.rowHeight)

function onScroll(event) {
  scrollTop.value = event.target.scrollTop
  viewportHeight.value = event.target.clientHeight
}

function cell(row, column) {
  const value = row[column.key]
  if (value === null || value === undefined) return ''
  if (typeof value === 'boolean') return value ? '是' : '否'
  return String(value)
}
</script>

<template>
  <div ref="scroller" class="vt" @scroll="onScroll">
    <table class="grid">
      <colgroup>
        <col v-for="column in columns" :key="column.key"
             :style="column.width ? { width: column.width } : null" />
      </colgroup>
      <thead>
        <tr>
          <th v-for="column in columns" :key="column.key">{{ column.label }}</th>
        </tr>
      </thead>
    </table>
    <!-- 空态优先走插槽（富空态：插画+标题+CTA）；没插内容的调用方
         回退到 emptyText 纯文本 —— 兼容零改动的旧用法。 -->
    <div v-if="!rows.length" class="empty">
      <slot name="empty">
        <span class="dim">{{ emptyText }}</span>
      </slot>
    </div>
    <div v-else class="body" :style="{ height: totalHeight + 'px' }">
      <table class="grid rows" :style="{ transform: `translateY(${offsetY}px)` }">
        <!-- 与表头分开两张表（表头要 sticky），列宽必须用同一份 colgroup 对齐 -->
        <colgroup>
          <col v-for="column in columns" :key="column.key"
               :style="column.width ? { width: column.width } : null" />
        </colgroup>
        <tbody>
          <template v-for="row in windowRows" :key="row.__key">
            <!-- 分段行（0.19.0 域名分组）：`__group` 是行的**标记**不是列 ——
                 跨列一行、不吃点击（它没有 id，点了没有详情可开）。 -->
            <tr v-if="'__group' in row" class="group-row"
                :style="{ height: rowHeight + 'px' }">
              <td :colspan="columns.length">{{ row.__group }}</td>
            </tr>
            <tr
              v-else
              :class="{ selected: String(selectedKey) === String(row.__key) }"
              :style="{ height: rowHeight + 'px' }"
              @click="emit('row-click', row)"
            >
              <td v-for="column in columns" :key="column.key"
                  :class="{ mono: column.mono }" :title="cell(row, column)">
                <slot :name="column.key" :row="row">{{ cell(row, column) }}</slot>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.vt {
  height: 100%;
  overflow: auto;
  position: relative;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.body { position: relative; }
.grid { table-layout: fixed; }
.rows { position: absolute; top: 0; left: 0; width: 100%; }
.group-row td {
  padding: 0 8px;
  background: var(--panel-2);
  border-bottom: 1px solid var(--line);
  color: var(--dim);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
}
.empty { padding: 18px; text-align: center; font-size: 12px; }
.empty:deep(.empty-state) { padding-top: 44px; padding-bottom: 44px; }
</style>
