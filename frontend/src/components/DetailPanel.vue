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
 * 本阶段详情面板是**只读 + 标注 + 三个实体操作**（3.8）：标签可以加也可以删
 * （`PATCH /entities/{id}` 的 add_tags / remove_tags）；**字段的值不能改** ——
 * 那条路由今天返回 501，理由是"人工写值"必须走 `ManualWriter` 的值写入路径
 * （2.6）。界面把这件事说出来，而不是给一个点了报错的输入框。
 *
 * 3.8 加的三件事都**不是**在界面里拼出来的：
 *
 *   改状态   → `store.setLifecycle`（`POST /entities/lifecycle`，一个 job）
 *   合并     → `store.mergeEntities`（`ops.merge`，可 unmerge）
 *   拆开     → `store.unmergeEntity`（`ops.unmerge`，天然精确可逆）
 *
 * 合并的**幸存者由人指定**（"合并到这个"按钮），不让界面去猜谁更像主 ——
 * 猜错的合并比没合并糟得多，因为它的症状要等到有人想拆开时才出现。
 */
import { computed, ref } from 'vue'
import { store } from '../store.js'
import { kindColor } from '../graph.js'

const detail = computed(() => store.selectedDetail)
/** 这个实体参与过的、仍然生效的合并事件。**单独一条读**
 *  （`GET /entities/{id}/merges`）而不是塞进 `/entities/{id}` —— 后者的形状
 *  是对外契约，3.8 不动它。 */
const merges = computed(() => store.selectedMerges || [])
const openGroups = ref({ tool: true, manual: true, imported: true })
const newTag = ref('')
const tagError = ref('')
const opError = ref('')
const busy = ref(false)

/** 合并的候选：当前**批量选中**里除了自己以外的那些实体（3.8）。
 *  批量选中是画布上 `Shift` 出来的东西，与"批量改状态"用的是同一个集合 ——
 *  两套选中状态迟早会分叉，而分叉的症状是"合并进去的不是我圈的那些"。 */
const mergeTargets = computed(() => {
  const self = detail.value?.entity?.id
  return (store.selection || []).filter((id) => id !== self)
})

const LIFECYCLE_LABEL = { active: '在用', retired: '已退休' }

async function run(fn, what) {
  if (busy.value) return
  busy.value = true
  opError.value = ''
  try {
    await fn()
  } catch (err) {
    opError.value = `${what}失败：${err.message}`
  } finally {
    busy.value = false
  }
}

function mergeHere() {
  const entity = detail.value?.entity
  if (!entity || !mergeTargets.value.length) return
  // 成员的 key 从画布上的节点取 —— 那是**界面上看得见的那个 key**，
  // 而不是再从别处查一遍（查出来的可能与眼前这张卡片不是同一个东西）。
  const keys = mergeTargets.value.map((id) => {
    const node = store.visible.nodes.find((n) => Number(n.data?.entityId) === Number(id))
    return node ? String(node.data.key) : null
  }).filter(Boolean)
  if (keys.length !== mergeTargets.value.length) {
    opError.value = '有些被选中的实体现在不在画布上（受筛选或载入上限影响）—— ' +
      '合并必须在看得见它们的时候做'
    return
  }
  return run(() => store.mergeEntities({
    survivorId: entity.id, survivorKey: entity.entity_key, memberKeys: keys,
  }), '合并')
}

function unmerge(eventId) {
  return run(() => store.unmergeEntity(detail.value.entity.id, eventId), '拆开')
}

function setLifecycle(entityId, lifecycle) {
  return run(() => store.setLifecycle([entityId], lifecycle), '改状态')
}

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
    await store.setEntityTags(detail.value.entity.id, { add: [tag] })
    newTag.value = ''
  } catch (err) {
    tagError.value = err.message
  }
}

/** 删一个标签。**删除也留审计**（v3.2 §6.1）—— `tag_events` 上那条 remove
 *  行与 `activity_log` 的 `tag.remove` 都在数据层写，界面这一侧什么都不用做。 */
async function removeTag(tag) {
  tagError.value = ''
  try {
    await store.setEntityTags(detail.value.entity.id, { remove: [tag] })
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
          <span v-for="tag in detail.tags" :key="tag" class="chip removable">
            {{ tag }}
            <button class="x" title="删掉这个标签（删除也留审计）"
                    @click="removeTag(tag)">×</button>
          </span>
          <span v-if="!detail.tags.length" class="faint small">（无）</span>
        </div>
        <div class="addtag">
          <input v-model="newTag" list="tag-vocab" placeholder="加一个标签（标注）"
                 @keydown.enter="addTag" />
          <!-- 词表来自 `GET /tags`（**库里的**数），不是"界面上见过的那些"：
               一个标签从所有实体上摘掉之后，它下一次就只能靠人记得重新敲。 -->
          <datalist id="tag-vocab">
            <option v-for="row in store.tags" :key="row.tag" :value="row.tag">
              {{ row.count }} 个实体
            </option>
          </datalist>
          <button class="ghost" @click="addTag">加</button>
        </div>
        <p v-if="tagError" class="bad small">{{ tagError }}</p>
        <p class="faint small">
          只能改标签与人工保护标记。**人工写入字段的值**走 2.6 的写路径，
          目前 `PATCH /entities/{id}` 对它返回 501。
        </p>
      </section>

      <section>
        <h2 class="section">状态</h2>
        <div class="acts">
          <button v-for="value in ['active', 'retired']" :key="value" class="ghost"
                  :class="{ on: detail.entity.lifecycle === value }"
                  :disabled="busy || detail.entity.lifecycle === value"
                  @click="setLifecycle(detail.entity.id, value)">
            {{ LIFECYCLE_LABEL[value] }}
          </button>
        </div>
        <p class="faint small">
          改状态是**一个 job**（一次事务，要么全成要么全不成），撤销按钮就在
          作业历史里 —— 与其它写操作是同一个。
        </p>
      </section>

      <section>
        <h2 class="section">合并（{{ merges.length }}）</h2>
        <ul class="merges">
          <li v-for="event in merges" :key="event.event_id">
            <span class="mono">#{{ event.event_id }}</span>
            <span class="mono">← {{ (event.member_keys || []).join(' / ') }}</span>
            <button class="ghost" :disabled="busy" @click="unmerge(event.event_id)">
              拆开
            </button>
          </li>
          <li v-if="!merges.length" class="dim">（没合并过）</li>
        </ul>
        <div class="acts">
          <button class="ghost" :disabled="busy || !mergeTargets.length"
                  :title="mergeTargets.length ? '' : '先在画布上按住 Shift 选中要并入的卡片'"
                  @click="mergeHere">
            把选中的 {{ mergeTargets.length }} 个并入这里
          </button>
        </div>
        <p class="faint small">
          合并是**身份覆盖**：事实一条都不移动，所以拆开天然精确可逆。它走
          `ops.merge` / `ops.unmerge` —— 在界面里拼一个"把两张卡片画成一张"
          会同时丢掉可逆性与冲突登记，而症状要等到有人想拆开时才出现。
        </p>
      </section>
      <p v-if="opError" class="bad small">{{ opError }}</p>

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
.chip.removable { display: inline-flex; align-items: center; gap: 3px; padding-right: 2px; }
.chip .x {
  border: 0; background: transparent; color: var(--faint); cursor: pointer;
  font-size: 12px; line-height: 1; padding: 0 3px;
}
.chip .x:hover { color: var(--bad, #F03E3E); }
.acts { display: flex; flex-wrap: wrap; gap: 6px; }
.acts button.on { border-color: var(--accent); color: var(--accent); }
.merges { list-style: none; margin: 0 0 8px; padding: 0; font-size: 11px; }
.merges li { display: flex; align-items: center; gap: 7px; padding: 3px 0; }
.merges li span { color: var(--dim); word-break: break-all; }
.timeline { list-style: none; margin: 0; padding: 0; font-size: 11px; }
.timeline li { display: flex; gap: 7px; padding: 2px 0; }
.timeline .at { color: var(--faint); font-size: 10px; }
.timeline .action { color: var(--dim); }
</style>
