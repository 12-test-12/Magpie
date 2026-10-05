<script setup>
/**
 * 左栏：项目总览 + 镜头切换 + 资产规模 + 生效筛选 + 管线列表（计划书 §7.1）。
 *
 * 类型徽标的数字来自 `GET /lenses/{lens}`（**库里的**数），不是当前画布上
 * 载入了几个。两者的差别在项目大的时候会非常刺眼：画布受 `limit=500` 限制，
 * 拿它当"库里有多少"就是在给一个偏小的假数字。徽标显示的是**库里的**，
 * 而画布上的聚合横幅说的是**这一次载入了多少** —— 两个口径各自说清楚。
 *
 * 管线列表（§7.1 的那一格）从 3.7b 起是**真的**：`GET /pipelines` 不再是 501。
 *
 * 列表里的数是**库里的**（`step_count` 来自定义本身），与镜头徽标同一条口径。
 *
 * 0.20.0 D 的三处改版（GPT 评审 ②③⑤⑥）：
 *
 *   * 顶部一行**项目总览**（实体 = `store.projectTotal`，库口径）——左栏从此
 *     有个"规模"的锚点，下面的比例条都以它为背景；
 *   * 类型区从胶囊平铺改成**按数量降序的行**：色点 + 名称 + 千分位数量 +
 *     比例条（宽度 = count/max，kind 色，非零保底 10%）—— 一眼看出构成；
 *   * **生效中的筛选可见**：类型 / 域名视图 / 关键词谁在生效谁就摆出来，
 *     逐条可摘 + 「重置全部」，替代只有「清除筛选（N）」的括号。
 *
 * ⚠️ 镜头清单**不在这里写死**：有哪几个镜头由 `kind_registry.lens` 决定
 * （`store.lensRows` 从 `/meta` 推导）。3.2 之前这里是 `['assets','services']`
 * 两个字面量，于是"登记了 `vuln` kind"不会让漏洞镜头出现 —— 数据在库里、
 * 界面里没有那一页，而唯一的修法是有人记得来改这个文件。
 *
 * 本轮（GPT 评审五/六）两处：
 *   * 导航按 **攻击面 / 安全 / 分析 / 任务** 四组（`NAV_GROUP` 是展示元数据，
 *     页清单仍只有 `store.VIEWS` + 未登记的镜头页一份）；
 *   * 资产规模分「攻击面 / 安全」两子标（`SECURITY_KINDS`），比例条分母是
 *     **全体**最大值 —— 跨组同一把尺。
 */
import { computed } from 'vue'
import { store } from '../store.js'
import { kindColor } from '../graph.js'
import { fmtInt } from '../format.js'

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

// ---------------------------------------------------------------------------
// 导航四组（评审五）：攻击面 / 安全 / 分析 / 任务 —— "一眼知道每个入口干什么"。
// ---------------------------------------------------------------------------
//
// 页清单**仍然只有 `store.VIEWS` 一份**（外加镜头页里 VIEWS 没登记的
// api/traffic/graph —— 它们是真页面，App 那边按 `lensKeys` 动态接线）。
// 这里的映射只是"这个页面归哪一组"的**展示元数据**：分组乱了顶多难找，
// 页面该开还是开 —— 与 `VIEWS.group`（顶栏分隔线）是两个用途，互不读取。
const NAV_GROUP = {
  explore: 'attack', assets: 'attack', services: 'attack',
  api: 'attack', graph: 'attack',
  traffic: 'analysis',
  vuln: 'security', risk: 'security', inbox: 'security',
  doctor: 'analysis',
  // 0.21.0：MCP 接入归「分析」—— 它是给 AI/人看这份库的入口，不是攻击面。
  // 漏映射的键会默认落进第一个组（攻击面），页面会静默站错队。
  mcp: 'analysis',
  jobs: 'tasks', pipeline: 'tasks',
}
const NAV_GROUP_LABELS = [
  { key: 'attack', label: '攻击面' },
  { key: 'security', label: '安全' },
  { key: 'analysis', label: '分析' },
  { key: 'tasks', label: '任务' },
]

const navGroups = computed(() => {
  const groups = NAV_GROUP_LABELS.map((g) => ({ ...g, items: [] }))
  const byKey = new Map(groups.map((g) => [g.key, g]))
  const seen = new Set()
  const push = (item) => {
    if (item.key === 'overview' || seen.has(item.key)) return  // 总览是头部那一行
    seen.add(item.key)
    //: 未登记的新页面/新镜头默认进「攻击面」—— 新东西落在有"资产"语义的组里，
    //: 比凭空消失或落进错误的组都强（改这里的映射是一行展示元数据的事）。
    const group = byKey.get(NAV_GROUP[item.key] || 'attack')
    group.items.push(item)
  }
  for (const item of store.VIEWS) {
    const lens = lensRows.value.find((row) => row.key === item.key)
    push({ key: item.key, label: lens ? lens.label : item.label, hint: item.hint,
           badge: lens && lens.data ? fmtInt(lens.data.total) : '' })
  }
  // VIEWS 没登记的镜头页（api / traffic / graph…）：真页面、真计数 —— 漏掉
  // 它们就是"库里有 endpoint 9,593，导航里没有接口这一页"。
  for (const row of lensRows.value) {
    push({ key: row.key, label: row.label, hint: `镜头：${row.label}`,
           badge: row.data ? fmtInt(row.data.total) : '' })
  }
  return groups.filter((group) => group.items.length)
})

// ---------------------------------------------------------------------------
// 资产规模两组（评审六）：攻击面 / 安全 —— 从"资产数据库"读成"安全资产平台"。
// ---------------------------------------------------------------------------
//: 安全组 = secret、vuln（界面展示元数据，同 `LENS_LABELS` 的先例）。
//: 未登记的新 kind **默认进攻击面组** —— 漏展示比分错组糟（分错组只是语义
//: 不准，漏展示是数字凭空消失）。
const SECURITY_KINDS = new Set(['secret', 'vuln'])

/**
 * 资产规模行：跨镜头摊平 + 按数量降序。
 *
 * 摊平不会重复 —— 一个 kind 在 `kind_registry` 里只属于一个镜头。数量是
 * **库里的**（`/lenses` 的 count），与画布这次载入了几个是两个口径（文件头
 * 那条纪律）。排序把最大的顶上去，比例条的分母就是第一行的 count。
 */
const kindRows = computed(() => {
  const rows = []
  for (const row of lensRows.value) {
    for (const kind of (row.data && row.data.kinds) || []) {
      rows.push({ kind: kind.kind, count: Number(kind.count) || 0 })
    }
  }
  return rows.sort((a, b) => b.count - a.count || a.kind.localeCompare(b.kind))
})

//: 两组**各自**降序；比例条的分母仍是全体最大值（barStyle 取 kindRows[0]）——
//: 跨组同一把尺，Security 组的条不会因为组内最大而虚胖。
const attackRows = computed(() => kindRows.value.filter((row) => !SECURITY_KINDS.has(row.kind)))
const securityRows = computed(() => kindRows.value.filter((row) => SECURITY_KINDS.has(row.kind)))

/**
 * 比例条：宽度 = count / max（最大的占满整行），**非零保底 10%** ——
 * 数量悬殊时小类型的条只剩几个像素，保底让它还在视野里。色走 kind 自己的色。
 */
function barStyle(row) {
  const max = kindRows.value.length ? kindRows.value[0].count : 0
  const ratio = max > 0 && row.count > 0 ? row.count / max : 0
  return {
    width: ratio > 0 ? `${Math.max(ratio * 100, 10)}%` : '0%',
    background: kindColor(row.kind),
  }
}

/**
 * 生效中的筛选（GPT ③）。每条自带摘除动作，且**各走各的那条路**：
 * 类型 → `toggleKind`（服务端重载），域名 → `clearDomainFocus`（服务端重载），
 * 关键词是纯本地谓词 —— 摘它不用重载，`_visible` 会立刻重算。
 */
const activeFilters = computed(() => {
  const chips = []
  for (const kind of store.filter.kinds) {
    chips.push({ key: `kind:${kind}`, label: `类型 ${kind}`,
                 remove: () => store.toggleKind(kind) })
  }
  if (store.filter.underDomain) {
    chips.push({ key: 'domain', label: `域名 ${store.filter.underDomain}`,
                 remove: () => store.clearDomainFocus() })
  }
  if (store.filter.keyword) {
    chips.push({ key: 'keyword', label: `关键词 ${store.filter.keyword}`,
                 remove: () => store.setFilter({ keyword: '' }) })
  }
  return chips
})

//: 管线列表。**列表里的东西是库里的**（`GET /pipelines`），不是草稿 ——
//: 与镜头徽标"显示库里的数"同一条纪律。
const pipelines = computed(() => store.pipeline.list)

function openPipeline(id) {
  store.setView('pipeline')
  store.selectPipeline(id)
}
</script>

<template>
  <aside class="left">
    <!-- 项目总览（GPT ⑤）：一行库里的实体总数。数是 `/projects` 的 counts，
         与画布载入数无关（0.20.0 的三个口径见 store 注释）。
         可点 = 回项目总览页（评审十一：总览是首页，这一行是它的锚点）。 -->
    <header class="overview home" title="回到项目总览"
            @click="store.setView('overview')">
      <span class="faint">实体</span>
      <b>{{ fmtInt(store.projectTotal) }}</b>
    </header>

    <!-- 导航四组（评审五）：攻击面 / 安全 / 分析 / 任务。 -->
    <section v-for="group in navGroups" :key="'nav' + group.key">
      <h2 class="section">{{ group.label }}</h2>
      <ul class="list">
        <li
          v-for="item in group.items"
          :key="item.key"
          :class="{ active: store.view === item.key }"
          :title="item.hint"
          @click="openLens(item.key)"
        >
          <span>{{ item.label }}</span>
          <span v-if="item.badge" class="badge">{{ item.badge }}</span>
        </li>
      </ul>
    </section>

    <section>
      <h2 class="section">资产规模（点击筛选画布）</h2>
      <!-- 比例行（GPT ②）：色点 + 名称 + 千分位数量 + 比例条。点击行为与旧
           胶囊一字没变（仍走 toggleKind）—— 换的是呈现，不是语义。
           两组（评审六）：比例条分母是**全体**最大值，跨组同一把尺。 -->
      <div v-if="attackRows.length" class="kind-group">
        <div class="group-label">攻击面</div>
        <ul class="kinds">
          <li
            v-for="row in attackRows"
            :key="'k' + row.kind"
            class="krow"
            :class="{ active: kindFilter(row.kind) }"
            :title="kindFilter(row.kind) ? `取消筛选：${row.kind}` : `只看画布上的 ${row.kind}`"
            @click="store.toggleKind(row.kind)"
          >
            <span class="ktop">
              <span class="swatch" :style="{ background: kindColor(row.kind) }" />
              <span class="name mono">{{ row.kind }}</span>
              <span class="count">{{ fmtInt(row.count) }}</span>
            </span>
            <span class="bar"><i :style="barStyle(row)" /></span>
          </li>
        </ul>
      </div>
      <div v-if="securityRows.length" class="kind-group">
        <div class="group-label">安全</div>
        <ul class="kinds">
          <li
            v-for="row in securityRows"
            :key="'k' + row.kind"
            class="krow"
            :class="{ active: kindFilter(row.kind) }"
            :title="kindFilter(row.kind) ? `取消筛选：${row.kind}` : `只看画布上的 ${row.kind}`"
            @click="store.toggleKind(row.kind)"
          >
            <span class="ktop">
              <span class="swatch" :style="{ background: kindColor(row.kind) }" />
              <span class="name mono">{{ row.kind }}</span>
              <span class="count">{{ fmtInt(row.count) }}</span>
            </span>
            <span class="bar"><i :style="barStyle(row)" /></span>
          </li>
        </ul>
      </div>
      <p v-if="noKinds" class="dim empty">还没有登记的类型</p>
    </section>

    <!-- 生效中的筛选（GPT ③）：条件不再只藏在「清除筛选（N）」的括号里。
         没有生效条件时整个区块不出现 —— 常驻一个空的"筛选：无"只是噪音。 -->
    <section v-if="activeFilters.length">
      <h2 class="section">当前筛选</h2>
      <div class="chips">
        <button
          v-for="chip in activeFilters"
          :key="chip.key"
          type="button"
          class="chipbtn"
          :title="`去掉这条筛选：${chip.label}`"
          @click="chip.remove()"
        >
          {{ chip.label }}<span class="x">×</span>
        </button>
      </div>
      <button class="ghost clear" @click="store.clearFilter()">重置全部</button>
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
          <span class="badge">{{ fmtInt(item.step_count) }}</span>
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
  /* --chrome：与顶栏同一层，比画布略亮 —— 左栏从此读作「框架/导航」，
     不再和内容区黏在一起。真正的层级靠"底色差 + 右侧分割线"两件事。 */
  background: var(--chrome);
  /* 12 → 16：左栏原先贴着边一列挤；间距从此走阶梯 token（--sp-*），
     不再各处手写一个数。 */
  padding: var(--sp-4);
  overflow-y: auto;
}

/* 项目总览行（GPT ⑤）：标签 + 千分位大数，一眼一个"库里有多大"的锚点。 */
.overview {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  padding-bottom: var(--sp-3);
  margin-bottom: var(--sp-1);
  border-bottom: 1px solid var(--line);
  font-size: 12px;
}
.overview b {
  font-size: 16px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
/* 可点回总览：hover 提示它是入口，不是只读统计。 */
.overview.home { cursor: pointer; border-radius: var(--radius-ui); }
.overview.home:hover { background: var(--tint-hover); }

/* 资产规模的组标（评审六）：比 h2.section 低一档的分隔标签。 */
.kind-group + .kind-group { margin-top: var(--sp-3); }
.group-label {
  font-size: 10px;
  letter-spacing: .08em;
  color: var(--faint);
  padding: 0 var(--sp-2) 3px;
}

section + section { margin-top: var(--sp-4); }
/* 标题与它管的列表拉开 —— 全局 h2.section 的 8px 底距在左栏里读作黏连。 */
.left h2.section { margin-bottom: var(--sp-3); }
.list { list-style: none; margin: 0; padding: 0; }
.list li {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: var(--sp-1) var(--sp-2);
  border-radius: var(--radius-ui);
  cursor: pointer;
  font-size: 12px;
}
.list li:hover { background: var(--tint-hover); }
.list li.active { background: var(--tint-active); color: var(--accent); }
.list li .name { flex: 1; }

/* 资产规模行：上行（色点 + 名称 + 数量）+ 下行（比例条）。 */
.kinds { list-style: none; margin: 0; padding: 0; }
.krow {
  padding: 3px var(--sp-2) 4px;
  border-radius: var(--radius-ui);
  cursor: pointer;
}
.krow:hover { background: var(--tint-hover); }
.krow.active { background: var(--tint-active); }
.krow.active .count { color: var(--accent); }
.ktop {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  line-height: 18px;
}
.ktop .name { flex: 1; overflow: hidden; text-overflow: ellipsis; }
.ktop .count {
  font-variant-numeric: tabular-nums;
  color: var(--dim);
  font-size: 11px;
}
.swatch { width: 7px; height: 7px; border-radius: 2px; flex: none; }
/* 比例条：轨道 3px 淡底，填充走 kind 色（宽度在 barStyle 里算，非零保底 10%）。 */
.bar {
  display: block;
  height: 3px;
  margin-top: 3px;
  border-radius: 2px;
  background: rgba(255, 255, 255, .07);
  overflow: hidden;
}
.bar i { display: block; height: 100%; border-radius: 2px; opacity: .8; }

/* 生效筛选 chips：可摘的胶囊，× 是动作不是装饰。 */
.chips { display: flex; flex-wrap: wrap; gap: 5px; }
.chipbtn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 7px;
  border: 1px solid var(--accent);
  border-radius: 999px;
  background: var(--tint-accent);
  color: var(--accent);
  font-size: 11px;
  line-height: 17px;
  cursor: pointer;
}
.chipbtn:hover { border-style: dashed; }
.chipbtn .x { font-size: 12px; line-height: 1; opacity: .8; }

.empty { cursor: default; font-size: 12px; margin: var(--sp-1) 0 0; }
.clear { margin-top: var(--sp-2); width: 100%; }
.new { margin-top: var(--sp-2); width: 100%; }
.ell { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.notimpl {
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
  color: var(--faint);
}
</style>
