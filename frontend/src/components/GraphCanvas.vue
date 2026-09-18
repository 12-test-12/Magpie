<script setup>
/**
 * 探索模式画布 —— **只读**。
 *
 * 只读不是靠"记得别调用那几个函数"，而是靠**入口根本不存在**：
 *
 *   * `nodes-connectable="false"` / `edges-updatable="false"`：Vue Flow 不会
 *     让用户从节点上拉出一条线，也不会让已有的线被改接到别处；
 *   * 组件里**没有** `@connect` / `@nodes-change` 处理器 —— 没有回调，就没有
 *     "某个事件顺手改一下图"的地方；
 *   * `EntityNode` 里没有 `Handle`（见那个组件的注释）；
 *   * store 里没有加/删节点与边的导出（见 `store.js` 的文件头）。
 *
 * 这四层里只要有一层在，就没人能从这个界面造出一条数据 —— 而它们同时**都不**
 * 妨碍探索模式允许的四件事：拖位置 / 折叠 / 筛选 / 标注（计划书 §7.2）。
 *
 * 「探索模式」与将来的「编排模式」必须在**视觉上**可区分（同一条交付标准）：
 * 画布左上角常驻一个"只读"徽标，边框用中性色；编排模式将来会换成强调色边框
 * 与"可编辑"徽标。靠一个徽标区分听起来很薄，但它的作用是让人在拖完一个节点
 * 之后不产生"我刚才是不是改了数据"的疑问 —— 而这个疑问本身才是混乱的来源。
 */
import { computed, onMounted, onUnmounted } from 'vue'
import { VueFlow, useVueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import EntityNode from './EntityNode.vue'
import { AGGREGATE_THRESHOLD, relationColor } from '../graph.js'
import { store } from '../store.js'

const props = defineProps({
  focused: { type: [String, Number], default: null },
})

const emit = defineEmits(['select'])

const { fitView, onNodeDragStop, onEdgeClick } = useVueFlow()

const flowNodes = computed(() => store.visible.nodes)
const flowEdges = computed(() => store.visible.edges.map((edge) => ({
  ...edge,
  style: { stroke: relationColor(edge.data.rel) },
  labelStyle: { fill: 'var(--dim)' },
  animated: false,
})))

const isAggregate = computed(() => store.visible.aggregated)

onMounted(() => {
  onNodeDragStop(({ node }) => {
    if (node.id.startsWith('kind:')) return          // 聚合节点没有位置可存
    store.persistLayout(node.id, node.position)
  })
  onEdgeClick(({ edge }) => {
    store.pushLog(`关系 #${edge.id}：${edge.data.srcKey} --${edge.data.rel}--> ` +
      `${edge.data.dstKey}（置信度 ${edge.data.confidence}）`, 'activity')
  })
  window.addEventListener('keydown', onKey)
})

onUnmounted(() => window.removeEventListener('keydown', onKey))

function onKey(event) {
  const tag = (event.target && event.target.tagName) || ''
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return
  if (event.key === 'F' || event.key === 'f') {
    fitView({ padding: 0.2, duration: 200 })
  } else if (event.key === 'L' || event.key === 'l') {
    relayout()
  }
}

/**
 * 自动布局：只重排**没有持久化位置**的节点，拖过的永不动（计划书 §7.3）。
 *
 * 位置本来就是确定性的网格而不是力导向 —— 自用工具里"图每次打开都长得不一样"
 * 比"排得不够好看"糟得多，而 §7.3 自己就写了"之后永不动用户拖过的节点"。
 */
function relayout() {
  store.setFilter({ focusId: null })
  fitView({ padding: 0.2, duration: 200 })
  store.pushLog('已重新适应画布（持久化的位置不会被改动）', 'activity')
}

function onNodeClick({ node }) {
  emit('select', node.id)
  store.selectEntity(node.id)
}

function onNodeDoubleClick({ node }) {
  if (node.id.startsWith('kind:')) return
  if (String(store.filter.focusId) === String(node.data.entityId)) {
    store.setFilter({ focusId: null })
  } else {
    store.setFilter({ focusId: node.data.entityId, depth: 1 })
  }
}

defineExpose({ fitView })
</script>

<template>
  <div ref="host" class="canvas">
    <div class="mode-badge">
      <span class="lock">🔒</span>
      <span>探索模式 · 只读</span>
    </div>

    <div v-if="isAggregate" class="aggregate-banner">
      {{ store.counts.entities }} 个节点已按类型聚合为
      {{ flowNodes.length }} 个（阈值 {{ AGGREGATE_THRESHOLD }}）——
      单个类型内部不再逐个显示
    </div>

    <div v-if="store.loadError" class="overlay bad">
      读取图谱失败：{{ store.loadError }}
    </div>
    <div v-else-if="store.loading" class="overlay dim">载入中…</div>

    <VueFlow
      :nodes="flowNodes"
      :edges="flowEdges"
      :node-types="{ entity: EntityNode }"
      :nodes-draggable="true"
      :nodes-connectable="false"
      :edges-updatable="false"
      :elements-selectable="true"
      :select-nodes-on-drag="false"
      :min-zoom="0.1"
      :max-zoom="2.5"
      :fit-view-on-init="true"
      :default-viewport="{ x: 0, y: 0, zoom: 0.85 }"
      :pan-on-scroll="true"
      :pan-on-drag="[1, 2]"
      :zoom-on-double-click="false"
      :prevent-scrolling="true"
      class="flow"
      @node-click="onNodeClick"
      @node-double-click="onNodeDoubleClick"
    >
      <Background :gap="24" :size="1.2" pattern-color="#1B1B20" bg-color="#0E0E11" />
      <Controls :show-interactive="false" position="bottom-right" />
    </VueFlow>

    <div v-if="!store.loading && !flowNodes.length" class="overlay empty">
      <slot name="empty" />
    </div>
  </div>
</template>

<style scoped>
.canvas { position: relative; width: 100%; height: 100%; background: var(--bg); }
.flow { width: 100%; height: 100%; }
.mode-badge {
  position: absolute;
  top: 12px;
  left: 12px;
  z-index: 4;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: rgba(26, 26, 31, .88);
  color: var(--dim);
  font-size: 11px;
  pointer-events: none;
}
.lock { font-size: 10px; }
.aggregate-banner {
  position: absolute;
  top: 46px;
  left: 12px;
  z-index: 4;
  max-width: 60%;
  padding: 5px 10px;
  border: 1px solid var(--warn);
  border-radius: var(--radius-ui);
  background: rgba(26, 26, 31, .92);
  color: var(--warn);
  font-size: 11px;
}
.overlay {
  position: absolute;
  inset: 0;
  z-index: 3;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  background: rgba(14, 14, 17, .86);
  padding: 24px;
  text-align: center;
}
</style>
