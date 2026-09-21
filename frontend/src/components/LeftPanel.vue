<script setup>
/**
 * 左栏：镜头切换 + 类型面板 + 管线列表（计划书 §7.1）。
 *
 * 类型徽标的数字来自 `GET /lenses/{lens}`（**库里的**数），不是当前画布上
 * 载入了几个。两者的差别在项目大的时候会非常刺眼：画布受 `limit=500` 限制，
 * 拿它当"库里有多少"就是在给一个偏小的假数字。徽标显示的是**库里的**，
 * 而画布上的聚合横幅说的是**这一次载入了多少** —— 两个口径各自说清楚。
 *
 * 管线列表（§7.1 的那一格）从 3.7b 起是**真的**：`GET /pipelines` 不再是 501。
 * 在那之前这里写的是一句"501，不放假按钮"的说明 —— 一个空按钮比没有按钮更让
 * 人恼火（2.7 那个静态页的文件头就写着这条），而这一格一直是照着那条纪律留
 * 白的。现在它有了真列表，那句话就该走了。
 *
 * 列表里的数是**库里的**（`step_count` 来自定义本身），与镜头徽标同一条口径。
 *
 * ⚠️ 镜头清单**不在这里写死**：有哪几个镜头由 `kind_registry.lens` 决定
 * （`store.lensRows` 从 `/meta` 推导）。3.2 之前这里是 `['assets','services']`
 * 两个字面量，于是"登记了 `vuln` kind"不会让漏洞镜头出现 —— 数据在库里、
 * 界面里没有那一页，而唯一的修法是有人记得来改这个文件。
 */
import { computed } from 'vue'
import { store } from '../store.js'
import { kindColor } from '../graph.js'

const lensRows = computed(() => store.lensRows)

function isActiveLens(key) {
  return store.view === key
}

function openLens(key) {
  store.setView(key)
}

function kindFilter(kind) {
  return store.filter.kinds.includes(kind)
}

const noKinds = computed(() => !lensRows.value.some(
  (row) => row.data && row.data.kinds && row.data.kinds.length))

//: 管线列表。**列表里的东西是库里的**（`GET /pipelines`），不是草稿 ——
//: 与镜头徽标"显示库里的数"同一条纪律。这一步是 3.7b 把那句"501，不放假按钮"
//: 换成真列表的地方（3.7a §7 的留白项 4）。
const pipelines = computed(() => store.pipeline.list)

function openPipeline(id) {
  store.setView('pipeline')
  store.selectPipeline(id)
}
</script>

<template>
  <aside class="left">
    <section>
      <h2 class="section">镜头</h2>
      <ul class="list">
        <li
          v-for="row in lensRows"
          :key="row.key"
          :class="{ active: isActiveLens(row.key) }"
          @click="openLens(row.key)"
        >
          <span>{{ row.label }}</span>
          <span class="badge">{{ row.data ? row.data.total : '—' }}</span>
        </li>
      </ul>
    </section>

    <section>
      <h2 class="section">类型（点击筛选画布）</h2>
      <ul class="list">
        <template v-for="row in lensRows" :key="'k' + row.key">
          <li
            v-for="kind in (row.data ? row.data.kinds : [])"
            :key="kind.kind"
            class="kind"
            :class="{ active: kindFilter(kind.kind) }"
            @click="store.toggleKind(kind.kind)"
          >
            <span class="swatch" :style="{ background: kindColor(kind.kind) }" />
            <span class="name mono">{{ kind.kind }}</span>
            <span class="badge">{{ kind.count }}</span>
          </li>
        </template>
        <li v-if="noKinds" class="dim empty">还没有登记的类型</li>
      </ul>
      <button v-if="store.filter.kinds.length" class="ghost clear"
              @click="store.clearFilter()">
        清除筛选（{{ store.filter.kinds.length }}）
      </button>
    </section>

    <section>
      <h2 class="section">管线</h2>
      <ul class="list">
        <li
          v-for="item in pipelines"
          :key="item.id"
          :class="{ active: String(store.pipeline.id) === String(item.id) }"
          @click="openPipeline(item.id)"
        >
          <span class="name ell">{{ item.name }}</span>
          <span class="badge">{{ item.step_count }}</span>
        </li>
        <li v-if="!pipelines.length" class="dim empty">还没有存下来的管线</li>
      </ul>
      <button class="ghost new" @click="store.setView('pipeline')">
        ✎ 打开编排模式
      </button>
    </section>
  </aside>
</template>

<style scoped>
.left {
  width: 216px;
  flex: none;
  border-right: 1px solid var(--line);
  background: var(--panel);
  padding: 12px;
  overflow-y: auto;
}
section + section { margin-top: 16px; }
.list { list-style: none; margin: 0; padding: 0; }
.list li {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 5px 7px;
  border-radius: var(--radius-ui);
  cursor: pointer;
  font-size: 12px;
}
.list li:hover { background: #202530; }
.list li.active { background: #1D2733; color: var(--accent); }
.list li .name { flex: 1; }
.swatch { width: 8px; height: 8px; border-radius: 2px; flex: none; }
.empty { cursor: default; }
.clear { margin-top: 8px; width: 100%; }
.new { margin-top: 8px; width: 100%; }
.ell { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.notimpl {
  margin: 0;
  font-size: 11px;
  line-height: 1.5;
  color: var(--faint);
}
</style>
