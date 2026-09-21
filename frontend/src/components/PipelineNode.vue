<script setup>
/**
 * 步骤卡片（编排模式的节点）。
 *
 * 与 `EntityNode.vue` 的差别只有一处，而那一处就是这个模式的全部：**它有两个
 * `Handle`** —— 左进（依赖）、右出（被依赖）。探索模式的卡片上**没有** Handle，
 * 因为那里不存在"拉一条线"这件事（见 `EntityNode.vue` 的注释）。
 *
 * 卡片**不持有任何状态**：它渲染 `data`，手势通过 `emit` 回到 store。画布是受控
 * 的（冻结 F6），卡片是受控的画布的一部分。
 *
 * 尺寸固定，与实体卡片同宽 —— 两个模式的画布看起来才像同一个工具。
 */
import { Handle, Position } from '@vue-flow/core'
import { STEP_HEIGHT, STEP_WIDTH } from '../pipeline.js'

const props = defineProps({
  data: { type: Object, required: true },
})

const KIND_TONE = { file: '#F59F00', glob: '#4F8DF7', dir: '#37B24D' }
</script>

<template>
  <div
    class="step"
    :class="{ selected: data.selected, broken: !data.pattern }"
    :style="{ width: STEP_WIDTH + 'px', height: STEP_HEIGHT + 'px',
              '--tone': KIND_TONE[data.sourceKind] || '#8B93A7' }"
    :data-step-id="data.stepId"
    :title="`${data.sourceKind}: ${data.pattern || '（还没填）'}`"
  >
    <Handle type="target" :position="Position.Left" />
    <div class="body">
      <div class="head">
        <span class="dot" />
        <span class="id mono">{{ data.label }}</span>
        <span v-if="data.dryRun" class="chip warn" title="这一步只预演">干跑</span>
      </div>
      <div class="sub mono">{{ data.pattern || '（还没选来源）' }}</div>
      <div class="foot">
        <span class="chip">{{ data.parser || '自动仲裁' }}</span>
        <span class="chip kind">{{ data.sourceKind }}</span>
        <span v-if="data.dependsOn.length" class="chip">{{ data.dependsOn.length }} 依赖</span>
      </div>
    </div>
    <Handle type="source" :position="Position.Right" />
  </div>
</template>

<style scoped>
.step {
  position: relative;
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-left: 3px solid var(--tone);
  border-radius: var(--radius-card);
  overflow: hidden;
  cursor: pointer;
  user-select: none;
}
.step.selected { border-color: var(--accent); box-shadow: var(--shadow-select); }
.step.broken { border-style: dashed; }
.body {
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  height: 100%;
  padding: 9px 11px 8px;
}
.head { display: flex; align-items: center; gap: 6px; min-width: 0; }
.dot { width: 6px; height: 6px; border-radius: 50%; background: var(--tone, var(--faint)); flex: none; }
.id { font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
.sub {
  font-size: 11px;
  color: var(--dim);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.foot { display: flex; align-items: center; gap: 5px; }
.chip {
  font-size: 10px;
  padding: 1px 5px;
  border: 1px solid var(--line);
  border-radius: 999px;
  color: var(--dim);
}
.chip.warn { color: var(--warn); border-color: var(--warn); }
.chip.kind { color: var(--tone); border-color: var(--tone); }
</style>
