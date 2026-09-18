<script setup>
/**
 * 右栏：实体详情（计划书 §7.1 的"节点参数面板"）。
 *
 *   概况 / 观测 / 关系 / 时间线 / 标签，观测按 type 分组折叠
 *
 * 这里的「观测按 type 分组」用的是 `observations.origin`（tool / manual /
 * imported）—— 它是数据层里**真的**存在的分组，不是界面自己按字段名前缀猜的。
 * 猜一个分组出来会让人以为"这个组是数据层的概念"，而它不是。
 *
 * 本阶段详情面板是**只读 + 标注**：标签可以加（`PATCH /entities/{id}` 的
 * add_tags，2.7 就有）；**字段的值不能改** —— 那条路由今天返回 501，理由是
 * "人工写值"必须走 `ManualWriter` 的值写入路径（2.6）。界面把这件事说出来，
 * 而不是给一个点了报错的输入框。
 */
import { computed, ref } from 'vue'
import { store } from '../store.js'
import { kindColor } from '../graph.js'

const detail = computed(() => store.selectedDetail)
const openGroups = ref({ tool: true, manual: true, imported: true })
const newTag = ref('')
const tagError = ref('')

const ORIGIN_LABEL = { tool: '工具产出', manual: '人工写入', imported: '导入带入' }

const grouped = computed(() => {
  const buckets = new Map()
  for (const obs of (detail.value?.observations || [])) {
    const key = obs.origin || 'tool'
    if (!buckets.has(key)) buckets.set(key, [])
    buckets.get(key).push(obs)
  }
  return [...buckets.entries()].map(([origin, items]) => ({ origin, items }))
})

const outRelations = computed(() =>
  (detail.value?.relations || []).filter((r) => r.direction === 'out'))
const inRelations = computed(() =>
  (detail.value?.relations || []).filter((r) => r.direction === 'in'))

function toggleGroup(origin) {
  openGroups.value[origin] = !openGroups.value[origin]
}

async function addTag() {
  const tag = newTag.value.trim()
  if (!tag) return
  tagError.value = ''
  try {
    await store.tagEntity(detail.value.entity.id, tag)
    newTag.value = ''
  } catch (err) {
    tagError.value = err.message
  }
}

function goTo(key) {
  const node = store.visible.nodes.find(
    (n) => String(n.data?.key) === String(key) || String(n.id) === String(key))
  if (!node) {
    store.pushLog(`画布上找不到 ${key}（可能受载入上限或筛选影响）`, 'activity', 'warn')
    return
  }
  store.setFilter({ focusId: null })
  store.selectEntity(node.data.entityId ?? node.id)
}
</script>

<template>
  <aside class="right">
    <div v-if="!detail" class="dim pad">
      <p>点画布上的一个实体，这里显示它的概况 / 观测 / 关系 / 时间线。</p>
      <p class="faint small">
        聚合视图下（节点数超过 200）卡片代表一个**类型**，没有单个实体的详情 ——
        在左栏按类型筛选后即可回到逐个实体。
      </p>
    </div>

    <template v-else>
      <header class="head">
        <div class="title">
          <span class="swatch" :style="{ background: kindColor(detail.entity.kind) }" />
          <span class="mono key">{{ detail.entity.entity_key }}</span>
        </div>
        <div class="tags">
          <span class="chip accent">{{ detail.entity.kind }}</span>
          <span class="chip">{{ detail.entity.lifecycle }}</span>
          <span class="chip mono">#{{ detail.entity.id }}</span>
        </div>
      </header>

      <section>
        <h2 class="section">概况</h2>
        <dl class="kv">
          <dt>首次出现</dt><dd class="mono">{{ detail.entity.first_seen || '—' }}</dd>
          <dt>最近出现</dt><dd class="mono">{{ detail.entity.last_seen || '—' }}</dd>
          <dt>人工保护</dt>
          <dd class="mono">
            {{ detail.manual.protected_fields.length
              ? detail.manual.protected_fields.join(', ') : '（无）' }}
          </dd>
        </dl>
      </section>

      <section>
        <h2 class="section">观测（{{ detail.observations.length }}）</h2>
        <p v-if="!detail.observations.length" class="warn small">
          一条观测都没有 —— 这是一个**没测过**的实体。
        </p>
        <div v-for="group in grouped" :key="group.origin" class="group">
          <button class="group-head ghost" @click="toggleGroup(group.origin)">
            <span class="caret">{{ openGroups[group.origin] ? '▾' : '▸' }}</span>
            {{ ORIGIN_LABEL[group.origin] || group.origin }}
            <span class="badge">{{ group.items.length }}</span>
          </button>
          <ul v-show="openGroups[group.origin]" class="obs">
            <li v-for="obs in group.items" :key="obs.id">
              <span class="field mono">{{ obs.semantic_field }}</span>
              <span class="value mono">{{ obs.value }}</span>
              <span class="chip" :class="{
                ok: obs.confidence === 'high',
                warn: obs.confidence === 'low' || obs.confidence === 'conflict',
              }">{{ obs.confidence }}</span>
            </li>
          </ul>
        </div>
      </section>

      <section>
        <h2 class="section">关系（出 {{ outRelations.length }} / 入 {{ inRelations.length }}）</h2>
        <ul class="rels">
          <li v-for="rel in outRelations" :key="'o' + rel.id">
            <span class="arrow ok">→</span>
            <span class="mono verb">{{ rel.rel }}</span>
            <a class="mono other" @click="goTo(rel.other_key)">{{ rel.other_key }}</a>
          </li>
          <li v-for="rel in inRelations" :key="'i' + rel.id">
            <span class="arrow dim">←</span>
            <span class="mono verb">{{ rel.rel }}</span>
            <a class="mono other" @click="goTo(rel.other_key)">{{ rel.other_key }}</a>
          </li>
          <li v-if="!detail.relations.length" class="dim">（无）</li>
        </ul>
      </section>

      <section>
        <h2 class="section">标签</h2>
        <div class="tags">
          <span v-for="tag in detail.tags" :key="tag" class="chip">{{ tag }}</span>
          <span v-if="!detail.tags.length" class="faint small">（无）</span>
        </div>
        <div class="addtag">
          <input v-model="newTag" placeholder="加一个标签（标注）" @keydown.enter="addTag" />
          <button class="ghost" @click="addTag">加</button>
        </div>
        <p v-if="tagError" class="bad small">{{ tagError }}</p>
        <p class="faint small">
          只能改标签与人工保护标记。**人工写入字段的值**走 2.6 的写路径，
          目前 `PATCH /entities/{id}` 对它返回 501。
        </p>
      </section>

      <section>
        <h2 class="section">时间线</h2>
        <ul class="timeline">
          <li v-for="row in detail.timeline" :key="row.id">
            <span class="mono at">{{ row.at }}</span>
            <span class="mono action">{{ row.action }}</span>
            <span class="faint">{{ row.actor }}</span>
          </li>
          <li v-if="!detail.timeline.length" class="dim">（无）</li>
        </ul>
      </section>
    </template>
  </aside>
</template>

<style scoped>
.right {
  width: 330px;
  flex: none;
  border-left: 1px solid var(--line);
  background: var(--panel);
  padding: 12px;
  overflow-y: auto;
}
.pad { padding: 8px; }
.small { font-size: 11px; line-height: 1.5; }
section + section { margin-top: 14px; }
.head { margin-bottom: 12px; }
.title { display: flex; align-items: center; gap: 7px; margin-bottom: 6px; }
.key { font-size: 13px; word-break: break-all; }
.swatch { width: 9px; height: 9px; border-radius: 2px; flex: none; }
.tags { display: flex; flex-wrap: wrap; gap: 5px; }
.kv { display: grid; grid-template-columns: 68px 1fr; gap: 3px 8px; margin: 0; font-size: 11px; }
.kv dt { color: var(--faint); }
.kv dd { margin: 0; word-break: break-all; }
.group { margin-bottom: 4px; }
.group-head { width: 100%; text-align: left; display: flex; align-items: center;
              gap: 6px; padding: 3px 4px; color: var(--dim); }
.caret { width: 9px; }
.obs { list-style: none; margin: 2px 0 6px; padding: 0 0 0 14px; }
.obs li { display: flex; align-items: center; gap: 6px; padding: 3px 0; font-size: 11px; }
.obs .field { color: var(--faint); min-width: 92px; }
.obs .value { flex: 1; word-break: break-all; }
.rels { list-style: none; margin: 0; padding: 0; font-size: 11px; }
.rels li { display: flex; align-items: center; gap: 6px; padding: 3px 0; }
.rels .verb { color: var(--dim); min-width: 74px; }
.rels .other { color: var(--accent); cursor: pointer; word-break: break-all; }
.rels .other:hover { text-decoration: underline; }
.addtag { display: flex; gap: 6px; margin-top: 8px; }
.addtag input { flex: 1; }
.timeline { list-style: none; margin: 0; padding: 0; font-size: 11px; }
.timeline li { display: flex; gap: 7px; padding: 2px 0; }
.timeline .at { color: var(--faint); font-size: 10px; }
.timeline .action { color: var(--dim); }
</style>
