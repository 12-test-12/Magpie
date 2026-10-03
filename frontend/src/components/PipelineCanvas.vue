<script setup>
/**
 * 编排模式画布 —— **可编辑**，而"可编辑"只意味着**一条边**。
 *
 * 3.7a 冻结 F7 把这件事说死了：**画布手势只表达依赖（边）**。增删步骤走左侧/右
 * 侧面板的按钮（`store.addStep` / `store.removeStep`），它们是**表单动作**，不
 * 经过 Vue Flow 的可变 API。所以这个组件里与"改图"有关的名字只有一个 ——
 * `onConnect`，也就是"用户从一个 Handle 拖到另一个 Handle"。
 *
 * 这不是抠门，是算出来的：`check.py` 的 READONLY 红线收窄成**文件级 × 名字级**
 * 白名单，而白名单里就只有这一个文件里的这一个名字（见 `check.py` 的
 * `_EDITABLE_CANVAS`）。**一个手势换一个洞，这笔账划得来。**
 *
 * 受控（冻结 F6）体现在这里：`flowNodes` / `flowEdges` 是 `computed`，来源是
 * `store.pipelineFlow`，而后者由 `store.pipeline.definition` 算出来。组件**不
 * 持有自己的图状态** —— 让 Vue Flow 自己持图会引入第二个事实来源，而两份状态
 * 之间同步的 bug 没有任何症状，直到有人保存了一份少了一块的管线。
 *
 * 与探索模式必须在**视觉上**可区分，而且那个区分要是一条**可断言的属性**而不是
 * 两段 CSS：根元素上的 `data-mode="pipeline"`（`GraphCanvas.vue` 上是
 * `"explore"`）。靠一个徽标区分听起来很薄，但它的作用是让人在拖完一个节点之后
 * 不产生"我刚才是不是改了数据"的疑问 —— 而这个疑问本身才是混乱的来源。
 */
import { computed, onMounted } from 'vue'
import { VueFlow, useVueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import PipelineNode from './PipelineNode.vue'
import { canConnect } from '../pipeline.js'
import { store } from '../store.js'

const emit = defineEmits(['select'])

//: **唯一**一个从 Vue Flow 拿的可变 API 名字。连线本身不走它 —— 事件被翻译成
//: `store.connectSteps`，改的是**定义**，节点与边随后由 `computed` 重新算出来。
const { onConnect, fitView } = useVueFlow()

const flowNodes = computed(() => store.pipelineFlow.nodes)
const flowEdges = computed(() => store.pipelineFlow.edges)

onMounted(() => {
  onConnect((params) => {
    const check = canConnect(store.pipeline.definition, params.source, params.target)
    if (!check.ok) {
      // 连不成的时候**说清楚为什么**，而不是让线弹回去什么都不发生 ——
      // 后者会被读成"这个界面有 bug"。
      store.pushLog(`连不上：${check.why}`, 'pipeline', 'warn')
      return
    }
    store.connectSteps(params.source, params.target)
  })
})

function onNodeClick({ node }) {
  emit('select', node.id)
  store.selectStep(node.id)
}
</script>

<template>
  <div class="canvas" data-mode="pipeline">
    <div class="mode-badge">
      <span class="pen">✎</span>
      <span>编排模式 · 可编辑</span>
    </div>

    <VueFlow
      :nodes="flowNodes"
      :edges="flowEdges"
      :node-types="{ step: PipelineNode }"
      :nodes-draggable="true"
      :nodes-connectable="true"
      :edges-updatable="false"
      :elements-selectable="true"
      :select-nodes-on-drag="false"
      :min-zoom="0.1"
      :max-zoom="2.5"
      :fit-view-on-init="true"
      :default-viewport="{ x: 0, y: 0, zoom: 0.9 }"
      :pan-on-scroll="true"
      :pan-on-drag="[1, 2]"
      :zoom-on-double-click="false"
      :prevent-scrolling="true"
      class="flow"
      @node-click="onNodeClick"
    >
      <Background :gap="24" :size="1.2" pattern-color="#1B1B20" bg-color="#0E0E11" />
      <Controls :show-interactive="false" position="bottom-right" />
    </VueFlow>

    <div v-if="!flowNodes.length" class="overlay empty">
      <slot name="empty" />
    </div>
  </div>
</template>

<style scoped>
.canvas { position: relative; width: 100%; height: 100%; background: var(--bg); }
.flow { width: 100%; height: 100%; }
/* 编排模式的边框用强调色 —— 与探索模式的中性色边框成对（§3.4）。
   `data-mode` 是可断言的那一半；这一段只是让人眼看得出。 */
.canvas[data-mode='pipeline'] { box-shadow: inset 0 0 0 1px var(--accent); }
.mode-badge {
  position: absolute;
  top: 12px;
  left: 12px;
  z-index: 4;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border: 1px solid var(--accent);
  border-radius: 999px;
  background: var(--overlay);
  color: var(--accent);
  font-size: 11px;
  pointer-events: none;
}
.pen { font-size: 10px; }
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
