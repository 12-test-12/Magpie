<script setup>
/**
 * Ctrl+K 的搜索面板。
 *
 * ⚠️ **它搜的是已载入画布上的实体，不是整库。** `GET /api/v1/search` 今天返回
 * **501**（计划书 §8 的查询 DSL 与 FTS5 尚未开工），所以这里没有假装自己在
 * 搜全库 —— 面板底部常驻一行说明，写着"只在已载入的 N 个实体里搜"。
 *
 * 为什么不干脆不提供搜索：计划书 §7.1 的外壳里有它，而**一个能用的局部搜索
 * 远比一个 501 弹窗有用** —— 前提是它说清楚自己搜的是哪儿。把"局部"说成
 * "全局"才是这里唯一不可接受的做法。
 */
import { computed, nextTick, ref, watch } from 'vue'
import { store } from '../store.js'
import { kindColor } from '../graph.js'

const keyword = ref('')
const active = ref(0)
const input = ref(null)

const results = computed(() => {
  const needle = keyword.value.trim().toLowerCase()
  const nodes = store.visible.nodes.filter((n) => !String(n.id).startsWith('kind:'))
  if (!needle) return nodes.slice(0, 12)
  return nodes
    .filter((n) => String(n.data.key || '').toLowerCase().includes(needle))
    .slice(0, 40)
})

watch(() => store.paletteOpen, async (open) => {
  if (open) {
    keyword.value = ''
    active.value = 0
    await nextTick()
    if (input.value) input.value.focus()
  }
})

function move(step) {
  if (!results.value.length) return
  active.value = (active.value + step + results.value.length) % results.value.length
}

function choose(node) {
  if (!node) return
  store.selectEntity(node.data.entityId ?? node.id)
  store.paletteOpen = false
}
</script>

<template>
  <div v-if="store.paletteOpen" class="scrim" @click.self="store.paletteOpen = false">
    <div class="palette panel">
      <input
        ref="input"
        v-model="keyword"
        placeholder="搜实体 key（子串）"
        @keydown.down.prevent="move(1)"
        @keydown.up.prevent="move(-1)"
        @keydown.enter.prevent="choose(results[active])"
        @keydown.esc.prevent="store.paletteOpen = false"
      />
      <ul class="results">
        <li
          v-for="(node, index) in results"
          :key="node.id"
          :class="{ active: index === active }"
          @mouseenter="active = index"
          @click="choose(node)"
        >
          <span class="swatch" :style="{ background: kindColor(node.data.kind) }" />
          <span class="mono key">{{ node.data.key }}</span>
          <span class="chip">{{ node.data.kind }}</span>
        </li>
        <li v-if="!results.length" class="dim none">没有匹配的实体</li>
      </ul>
      <details class="tip foot">
        <summary>搜索范围：只在已载入的 {{ store.counts.entities }} 个实体里</summary>
        <div class="body">
          <span class="mono">GET /api/v1/search</span>（计划书 §8 的结构化过滤 +
          FTS5 + 查询语法）目前返回 <b>501</b>，尚未开工 —— 所以这里搜的是画布上
          已载入的实体，不是整库。
        </div>
      </details>
    </div>
  </div>
</template>

<style scoped>
.scrim {
  position: fixed;
  inset: 0;
  z-index: 40;
  background: var(--scrim);
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding-top: 12vh;
}
.palette { width: 520px; padding: 10px; box-shadow: var(--raise); }
.results { list-style: none; margin: 8px 0 0; padding: 0; max-height: 46vh; overflow-y: auto; }
.results li {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: var(--radius-ui);
  cursor: pointer;
  font-size: 12px;
}
.results li.active { background: var(--tint-active); }
.results .key { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.results .none { cursor: default; }
.swatch { width: 8px; height: 8px; border-radius: 2px; flex: none; }
.foot { margin-top: 8px; }
</style>
