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
import { AGGREGATE_THRESHOLD, kindColor, relationColor } from '../graph.js'
import { fmtInt } from '../format.js'
import { store } from '../store.js'

const props = defineProps({
  focused: { type: [String, Number], default: null },
})

const emit = defineEmits(['select'])

const { fitView, onNodeDragStop, onEdgeClick } = useVueFlow()

//: 模板根元素的 ref —— 折叠事件的程序化监听挂在这里（见 onFoldEvent）。
const host = ref(null)

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

/**
 * 图例（视觉整改）。数据源是**当前渲染的节点**（`store.visible`）—— 图上画了
 * 什么，图例就列什么，两边不可能对不上（聚合模式下按卡片的 count 汇总）。
 * 点击 = `store.toggleKind`：与左栏「类型」胶囊同一个动作、同一套白名单语义，
 * 不是第二套筛选状态。
 */
const legendItems = computed(() => {
  const counts = new Map()
  for (const node of flowNodes.value || []) {
    const data = node.data || {}
    const kind = String(data.kind || '')
    if (!kind) continue
    const weight = data.aggregated && Number.isFinite(data.count) ? data.count : 1
    counts.set(kind, (counts.get(kind) || 0) + weight)
  }
  return [...counts.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([kind, count]) => ({ kind, count }))
})
const activeKinds = computed(() => store.filter.kinds || [])
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
    if (node.id.startsWith('kind:')) {
      // 聚合卡：只进会话记忆（进不了 entity_layout，见 store.moveAggregate）
      store.moveAggregate(node.id, node.position)
      return
    }
    store.persistLayout(node.id, node.position)
  })
  onEdgeClick(({ edge }) => {
    store.pushLog(`关系 #${edge.id}：${edge.data.srcKey} --${edge.data.rel}--> ` +
      `${edge.data.dstKey}（置信度 ${edge.data.confidence}）`, 'activity')
  })
  host.value?.addEventListener('toggle-collapse', onFoldEvent)
  window.addEventListener('keydown', onKey)
})

/**
 * 卡片上的折叠按钮 → `store.toggleCollapse`。
 *
 * **带上坐标**：折叠是"改这一列"，不是"建一条记录" —— 后端拒绝"对一条不存在的
 * 行只写 collapsed"，因为那条记录会带着 (0,0) 出生，节点下次打开就跳到左上角。
 *
 * 事件是 `EntityNode` 从卡片根元素 `dispatchEvent` 出来的**冒泡 DOM 事件**，
 * 这里用程序化 `addEventListener` 收 —— 而不是 `<VueFlow @toggle-collapse>`：
 * Vue Flow 渲染自定义节点时只传固定的上下文 props，不透传组件上的监听器，
 * 模板绑定收到的永远是空（这正是这个按钮曾经"点了没反应"的原因）。带连字符的
 * 事件名走模板绑定还有 Vue 编译歧义，程序化注册没有这层问题。
 *
 * 坐标取自 `store.visible` —— 它永远等于"屏幕上正在渲染的位置"（拖完已经由
 * `moveEntity` 同步），比事件携带的快照可靠。
 */
function onFoldEvent(event) {
  const card = event.target && event.target.closest
    ? event.target.closest('[data-entity-id]') : null
  if (!card) return
  const id = card.getAttribute('data-entity-id')
  if (id === null || id === '' || id === 'null') return   // 聚合卡没有身份
  const node = (store.visible.nodes || []).find((n) => String(n.id) === String(id))
  store.toggleCollapse(id, node ? node.position : undefined)
}

onUnmounted(() => {
  host.value?.removeEventListener('toggle-collapse', onFoldEvent)
  window.removeEventListener('keydown', onKey)
})

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
 *
 * 聚合卡（`kind:`）单击 = **下钻**（评审二/三：`domain × 30` 点开要看那 30 个
 * 域名）。聚合卡没有实体身份，所以它不是"选中"，是"展开这一类"——
 * `store.drillKind` 会按 kinds 服务端重载并旁路聚合。
 */
function onNodeClick({ node, event }) {
  if (node.id.startsWith('kind:')) {
    store.drillKind(node.data?.kind || node.id.slice('kind:'.length))
    return
  }
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
    <!-- title（GPT 轻量项）：只读徽标悬停即有解释 —— "只读"挡的是什么、放行
         什么，说清楚才不会让人怀疑"我刚才是不是改了数据"。 -->
    <div class="mode-badge"
         title="探索模式：拖位置 / 折叠 / 筛选 / 标注都可以；事实数据只由导入与作业写入，画布上改不动它">
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

    <!-- 三个口径一起说（0.20.0）：库中总数（符合当前筛选、无 LIMIT）/ 已载入 /
         聚合后的卡片数。原来只写"500 个节点已聚合"，与左栏的 9,593 对不上 ——
         500 是载入数，不是库。 -->
    <div v-if="isAggregate" class="aggregate-banner">
      库中共 {{ fmtInt(store.graphTotal) }} · 已载入 {{ fmtInt(store.counts.entities) }}
      个节点 → 聚合为 {{ flowNodes.length }} 张卡（阈值 {{ AGGREGATE_THRESHOLD }}）
      —— <b>点一张卡下钻到具体实体</b>
    </div>

    <!-- 下钻横幅（评审二/三）：常驻出口与三个口径 —— 与域名视图横幅同一纪律：
         任何"图变小/变样了"的状态都必须有可见的原因和回去的门。 -->
    <div v-if="store.drill" class="drill-banner">
      <span class="label">下钻</span>
      <span class="key mono">{{ store.drill }}</span>
      <span class="count">
        库中 {{ fmtInt(store.graphTotal) }} · 已逐个显示
        {{ fmtInt(store.counts.entities) }} 个
      </span>
      <button class="ghost" @click="store.exitDrill()">退出下钻</button>
    </div>

    <!-- 域名视图横幅（0.19.0）。裁剪是**服务端**做的，所以必须有个常驻的出口：
         否则"图怎么变小了"没有答案，也没有回去的门。顶部居中是因为左上被
         只读徽标/聚合条占着、右上是批量条 —— 三个都挤左边会叠成一团。 -->
    <div v-if="store.filter.underDomain" class="domain-banner">
      <span class="label">域名视图</span>
      <span class="key">{{ store.filter.underDomain }}</span>
      <span class="count">{{ fmtInt(store.graphTotal) }} 个实体</span>
      <button class="ghost" @click="store.clearDomainFocus()">退出</button>
    </div>

    <!-- 图例浮层（视觉整改）。左下角 —— 缩放控件被钉在右下（Controls
         position="bottom-right"），左下只属于图例。只有一种类型时不摆图例：
         一个没人会点的图例只是在占地方。 -->
    <div v-if="legendItems.length > 1" class="legend">
      <button v-for="item in legendItems" :key="item.kind"
              class="chip legend-chip"
              :class="{ on: activeKinds.includes(item.kind) }"
              @click="store.toggleKind(item.kind)">
        <span class="swatch" :style="{ background: kindColor(item.kind) }" />
        <span class="mono">{{ item.kind }}</span>
        <b>{{ item.count }}</b>
      </button>
    </div>

    <div v-if="store.loadError" class="overlay bad">
      读取图谱失败：{{ store.loadError }}
    </div>
    <div v-else-if="store.loading" class="overlay dim">载入中…</div>

    <!-- 交互（0.20.0 修"无法滑动"）：原来 pan-on-drag=[1,2] 把左键排除在平移
         之外（普通左键拖拽什么也不发生），pan-on-scroll 又让滚轮去平移而不是
         缩放 —— 图工具的标准手势两个都不成立。现在：空白处左键拖 = 平移，
         滚轮 = 缩放（zoom-on-scroll 默认 true），Shift 框选（默认值）。 -->
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
      :pan-on-drag="true"
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
  /* 纯提示条不拦指针：没有它，画布左上角一条区域的拖拽/点选全被吃掉 */
  pointer-events: none;
}
/* 下钻横幅：与聚合横幅同槽（两者互斥 —— 下钻中必然不是聚合态），但它带
   「退出下钻」按钮，所以**要**拦指针；这就是它不复用 aggregate-banner 的原因。 */
.drill-banner {
  position: absolute;
  top: 46px;
  left: 12px;
  z-index: 5;
  display: flex;
  align-items: center;
  gap: 8px;
  max-width: 70%;
  padding: 5px 10px;
  border: 1px solid var(--accent);
  border-radius: var(--radius-ui);
  background: var(--overlay);
  font-size: 11px;
}
.drill-banner .label { color: var(--dim); }
.drill-banner .key { color: var(--accent); font-weight: 600; }
.drill-banner .count { color: var(--dim); }
.legend {
  position: absolute;
  bottom: 12px;
  left: 12px;
  z-index: 4;
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  max-width: 46%;
  padding: 6px;
  border: 1px solid var(--line);
  border-radius: var(--radius-ui);
  background: var(--overlay);
}
.legend-chip { cursor: pointer; }
.legend-chip b { color: var(--fg); font-weight: 600; }
.legend-chip.on { border-color: var(--accent); color: var(--accent); }
.legend-chip.on b { color: var(--accent); }
.legend .swatch {
  width: 8px;
  height: 8px;
  border-radius: 2px;
  display: inline-block;
}
.domain-banner {
  position: absolute;
  top: 12px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 5;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 10px;
  border: 1px solid var(--accent);
  border-radius: 999px;
  background: var(--overlay);
  font-size: 11px;
  white-space: nowrap;
}
.domain-banner .label { color: var(--dim); }
.domain-banner .key { color: var(--accent); font-weight: 600; }
.domain-banner .count { color: var(--dim); }
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
/* 空态不再全屏压暗（GPT ①）：网点背景留着 —— 引导卡自己就是一张描边卡片
   （.guide.panel 的底色/描边/阴影），86% 的黑纱只把"这里为什么空"盖成了
   一屏灰。载入中 / 出错仍用重遮罩：那两个状态下没有可读的背景可言。 */
.overlay.empty { background: transparent; }
</style>
