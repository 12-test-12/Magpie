<script setup>
/**
 * 顶部外壳（计划书 §7.1 的映射）。
 *
 *   n8n 的「工作流名 + Save + Run」 → 项目切换 + 导入（=Run）+ 搜索 + 体检 + Inbox 徽标
 *
 * "Run" 的位置放**导入**，因为它就是唯一的"执行"动作 —— 这个工具不发起扫描，
 * 它把别的工具的**输出**收进来。把它叫 Run 还是叫导入不影响它排在最右边；
 * 别在它旁边再放一个"运行"，那会让人以为这里能发起扫描。
 */
import { store } from '../store.js'

defineProps({
  version: { type: String, default: '' },
})

const emit = defineEmits(['import', 'palette'])
</script>

<template>
  <header class="bar">
    <div class="brand">
      <span class="logo">Magpie</span>
      <span class="mono faint v">{{ version }}</span>
    </div>

    <label class="project">
      <span class="faint">项目</span>
      <select
        :value="store.project"
        @change="store.selectProject($event.target.value)"
      >
        <option v-if="!store.projects.length" value="">（还没有项目）</option>
        <option v-for="p in store.projects" :key="p.id" :value="p.name">
          {{ p.name }}（{{ p.counts.entities }} 实体）
        </option>
      </select>
    </label>

    <nav class="views">
      <button
        v-for="item in store.VIEWS"
        :key="item.key"
        class="ghost tab"
        :class="{ active: store.view === item.key }"
        :title="item.hint"
        @click="store.setView(item.key)"
      >
        {{ item.label }}
        <span v-if="item.key === 'inbox' && store.openInboxCount"
              class="badge hot">{{ store.openInboxCount }}</span>
      </button>
    </nav>

    <span class="spacer" />

    <button class="ghost" title="Ctrl+K" @click="emit('palette')">
      搜索 <span class="mono faint">Ctrl+K</span>
    </button>
    <button class="ghost" @click="store.setView('doctor')">体检</button>
    <button class="primary" @click="emit('import')">导入</button>

    <!--
      只读徽标**常驻**，不是悬停才出现。CLI 后置之后这是唯一的用户界面，
      而它本阶段只做只读探索 —— 与其让人拖完一个节点再怀疑"我是不是改了数据"，
      不如一开始就说清楚。
    -->
    <span class="chip" title="本阶段只做只读探索模式，编排模式尚未开工">🔒 只读</span>
  </header>
</template>

<style scoped>
.bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 14px;
  background: var(--panel);
  border-bottom: 1px solid var(--line);
  flex: none;
}
.brand { display: flex; align-items: baseline; gap: 8px; }
.logo { font-size: 15px; font-weight: 600; letter-spacing: .04em; }
.v { font-size: 11px; }
.project { display: flex; align-items: center; gap: 6px; font-size: 12px; }
.project select { min-width: 170px; }
.views { display: flex; gap: 2px; margin-left: 8px; }
.tab { border-color: transparent; color: var(--dim); display: inline-flex; gap: 5px; }
.tab.active { border-color: var(--accent); color: var(--accent); }
.spacer { flex: 1; }
</style>
