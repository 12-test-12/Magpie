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
import { computed } from 'vue'
import { store } from '../store.js'

defineProps({
  version: { type: String, default: '' },
})

/** 按 `group` 归组（保持 VIEWS 声明顺序），给模板用来插组间分隔线。 */
const viewGroups = computed(() => {
  const order = []
  const byGroup = {}
  for (const item of store.VIEWS) {
    const name = item.group || 'other'
    if (!byGroup[name]) {
      byGroup[name] = []
      order.push(name)
    }
    byGroup[name].push(item)
  }
  return order.map((name) => ({ name, items: byGroup[name] }))
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

    <!-- 按 `item.group` 分组、组间加分隔线：模式 / 镜头 / 治理。
         左栏是"当前上下文的二级导航"，顶栏是全局页 —— 分隔线让这层
         从属关系看得见，而不是两排长得一样的按钮互相抢。 -->
    <nav class="views">
      <template v-for="(group, gi) in viewGroups" :key="group.name">
        <span v-if="gi" class="divider" aria-hidden="true" />
        <button
          v-for="item in group.items"
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
      </template>
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
  background: var(--chrome);
  border-bottom: 1px solid var(--line);
  flex: none;
}
.brand { display: flex; align-items: baseline; gap: 8px; }
.logo { font-size: 15px; font-weight: 600; letter-spacing: .04em; }
.v { font-size: 11px; }
.project { display: flex; align-items: center; gap: 6px; font-size: 12px; }
.project select { min-width: 170px; }
.views { display: flex; align-items: center; gap: 2px; margin-left: 8px; }
/* 组间竖线 —— 「模式 | 镜头 | 治理」的层级一眼可分 */
.divider {
  width: 1px;
  height: 16px;
  background: var(--line);
  margin: 0 7px;
  flex: none;
}
.tab {
  border-color: transparent;
  color: var(--dim);
  display: inline-flex;
  gap: 5px;
  border-radius: 999px;
  padding: 4px 11px;
}
.tab:hover:not(:disabled) { background: var(--tint-hover); border-color: transparent; }
.tab.active {
  background: var(--tint-accent);
  border-color: transparent;
  color: var(--accent);
}
.spacer { flex: 1; }
</style>
