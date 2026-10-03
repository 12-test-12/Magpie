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
import { computed, onMounted, onUnmounted, ref } from 'vue'
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

/** 折叠节点的 id 集合。折叠是**渲染层**的事（计划书 §7.3 的"把噪音收起来"），
 *  它不动图的构成 —— 节点还在、边还在，只是画得轻一点。 */
const foldedIds = computed(() => new Set(
  (store.visible.nodes || [])
    .filter((node) => node.data && node.data.collapsed)
    .map((node) => String(node.id))))

const flowEdges = computed(() => store.visible.edges.map((edge) => {
  // 两端任意一端折叠了 → 这条线也淡下去。只淡卡片不淡线，会留下一堆"接不到
  // 任何东西"的线头，而那比不折叠更难读。
  const faded = foldedIds.value.has(String(edge.source))
    || foldedIds.value.has(String(edge.target))
  return {
    ...edge,
    style: { stroke: relationColor(edge.data.rel), opacity: faded ? 0.25 : 1 },
    labelStyle: { fill: 'var(--dim)', opacity: faded ? 0.3 : 1 },
    animated: false,
  }
}))

const isAggregate = computed(() => store.visible.aggregated)
const selection = computed(() => store.selection || [])
const busy = ref(false)
const batchNote = ref('')

/**
 * 批量改状态。**一次点击 = 一个 job**（`ops.set_lifecycle`），所以撤销它就是在
 * 作业历史里撤那一个 job ——"要么全成要么全不成"这条硬线落在这个形状上。
 */
async function batchLifecycle(lifecycle) {
  if (busy.value) return
  busy.value = true
  batchNote.value = ''
  try {
    const data = await store.setLifecycle(selection.value, lifecycle)
    batchNote.value = data
      ? `改了 ${data.counts.changed} 个（作业 ${data.job_id}，可在作业历史里撤销）`
      : '没有可改的实体'
    store.clearSelection()
  } catch (err) {
    batchNote.value = `批量改状态失败：${err.message}`
  } finally {
    busy.value = false
  }
}

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

/**
 * 卡片上的折叠按钮 → `store.toggleCollapse`。
 *
 * **带上坐标**：折叠是"改这一列"，不是"建一条记录" —— 后端拒绝"对一条不存在的
 * 行只写 collapsed"，因为那条记录会带着 (0,0) 出生，节点下次打开就跳到左上角。
 *
 * 事件经 Vue Flow 的**自定义节点事件转发**上来（自定义节点组件 `emit` 的名字，
 * 在 `<VueFlow>` 上用 `@toggle-collapse` 收）。这样卡片不必知道 store 长什么样。
 */
function onToggleCollapse({ node }) {
  if (!node || String(node.id).startsWith('kind:')) return
  store.toggleCollapse(node.id, node.position)
}

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

/**
 * 单击卡片 = 选中它。
 *
 * `Shift` + 单击 = **批量选中**（3.8）：那一组 id 就是详情面板里"把选中的并入
 * 这里"与批量改状态的操作对象。走的是浏览器原生的 `shiftKey`，不自己维护一个
 * "上一次点的是谁" —— 那个状态在切换筛选、切换项目、刷新之后都会说谎。
 */
function onNodeClick({ node, event }) {
  if (node.id.startsWith('kind:')) return               // 聚合节点没有身份
  if (event && event.shiftKey) {
    store.toggleSelection(node.data?.entityId ?? node.id)
    return
  }
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
  <!-- `data-mode` 是「两种模式真的不同」这条交付标准的**可断言形式**（3.7a
       §3.4）：`PipelineCanvas.vue` 的根元素上是 `"pipeline"`。靠一个徽标区分
       听起来很薄，但它的作用是让人在拖完一个节点之后不产生"我刚才是不是改了
       数据"的疑问 —— 而这个疑问本身才是混乱的来源。 -->
  <div ref="host" class="canvas" data-mode="explore">
    <div class="mode-badge">
      <span class="lock">🔒</span>
      <span>探索模式 · 只读</span>
    </div>

    <!-- 批量操作条（3.8）。它只在**选中了东西**的时候出现 —— 一个常驻的禁用
         工具条会让人问"它为什么灰着"，而灰着的按钮从来不是答案。 -->
    <div v-if="selection.length" class="batch-bar">
      <span class="count">已选 {{ selection.length }} 个</span>
      <button class="ghost" :disabled="busy" @click="batchLifecycle('retired')">
        标为已退休
      </button>
      <button class="ghost" :disabled="busy" @click="batchLifecycle('active')">
        标为在用
      </button>
      <button class="ghost" @click="store.clearSelection()">清空</button>
      <span v-if="batchNote" class="note">{{ batchNote }}</span>
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
      @toggle-collapse="onToggleCollapse"
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
  background: var(--overlay);
  color: var(--dim);
  font-size: 11px;
  pointer-events: none;
}
.lock { font-size: 10px; }
.batch-bar {
  position: absolute;
  top: 12px;
  right: 12px;
  z-index: 5;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 8px;
  border: 1px solid var(--accent);
  border-radius: var(--radius-ui);
  background: var(--overlay);
  font-size: 11px;
}
.batch-bar .count { color: var(--accent); }
.batch-bar .note { color: var(--dim); max-width: 280px; }
.aggregate-banner {
  position: absolute;
  top: 46px;
  left: 12px;
  z-index: 4;
  max-width: 60%;
  padding: 5px 10px;
  border: 1px solid var(--warn);
  border-radius: var(--radius-ui);
  background: var(--overlay);
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
  background: var(--scrim-heavy);
  padding: 24px;
  text-align: center;
}
</style>
