<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { store } from '../store.js'
import { params, requestSlot, workspacePages } from '../workspace.js'

const keyword = ref(''), active = ref(0), input = ref(null), data = ref(null)
const loading = ref(false), error = ref('')
const slot = requestSlot()
let timer, previousFocus
const results = computed(() => (data.value?.groups || []).flatMap(g => g.rows.map(row => ({ row, category: g.category }))))
function search() {
  slot.cancel(); data.value = null; error.value = ''; active.value = 0
  if (!store.paletteOpen || !store.project || !keyword.value.trim()) { loading.value = false; return }
  loading.value = true
  slot.run('/workspace/search?' + params({ project: store.project, q: keyword.value.trim(), limit: 5 }),
    result => { data.value = result }, text => { error.value = text }, () => { loading.value = false })
}
watch(keyword, () => {
  clearTimeout(timer); slot.cancel(); data.value = null; error.value = ''; loading.value = !!keyword.value.trim()
  timer = setTimeout(search, 250)
})
watch(() => store.paletteOpen, async open => {
  clearTimeout(timer); slot.cancel(); data.value = null; error.value = ''; loading.value = false
  if (open) {
    previousFocus = document.activeElement; keyword.value = ''; active.value = 0
    await nextTick(); input.value?.focus()
  } else previousFocus?.focus?.()
})
watch(() => store.project, () => { clearTimeout(timer); keyword.value = ''; search() })
function move(step) {
  if (results.value.length) active.value = (active.value + step + results.value.length) % results.value.length
  nextTick(() => document.querySelector('.palette .search-hit.active')?.scrollIntoView?.({ block: 'nearest' }))
}
function choose(hit) {
  if (!hit) return
  store.openWorkspace(hit.category, hit.row, { q: keyword.value.trim() })
  store.paletteOpen = false
}
function indexOf(category, row) { return results.value.findIndex(r => r.category === category && r.row.id === row.id) }
function trap(event) {
  const focusable = [...event.currentTarget.querySelectorAll('input,button')].filter(el => !el.disabled)
  const i = focusable.indexOf(document.activeElement)
  if (event.shiftKey && i <= 0) { event.preventDefault(); focusable.at(-1)?.focus() }
  else if (!event.shiftKey && i === focusable.length - 1) { event.preventDefault(); focusable[0]?.focus() }
}
onBeforeUnmount(() => { clearTimeout(timer); slot.cancel() })
</script>

<template>
  <div v-if="store.paletteOpen" class="scrim" @click.self="store.paletteOpen = false">
    <section class="palette panel" role="dialog" aria-modal="true" aria-label="全局快捷检索" @keydown.tab="trap" @keydown.esc.prevent="store.paletteOpen = false">
      <header><strong>全局检索 · {{ store.project || '尚未选择项目' }}</strong><button class="ghost" aria-label="关闭检索" @click="store.paletteOpen = false">✕</button></header>
      <input ref="input" v-model="keyword" aria-label="全局检索关键词" maxlength="300" placeholder="输入域名、接口路径、发现标题、文件名或笔记…"
        @keydown.down.prevent="move(1)" @keydown.up.prevent="move(-1)" @keydown.enter.prevent="choose(results[active])" />
      <div class="results" aria-live="polite">
        <p v-if="!store.project" class="dim">先选择项目。</p>
        <p v-else-if="loading" role="status">正在检索项目…</p>
        <div v-else-if="error" role="alert"><p>{{ error }}</p><button @click="search">重试</button></div>
        <p v-else-if="!keyword.trim()" class="dim">输入关键词，一次检索资产、API、Findings、Evidence 和 Notes。</p>
        <template v-else-if="data">
          <section v-for="group in data.groups" :key="group.category" class="search-group">
            <header><strong>{{ workspacePages[group.category].label }} · {{ group.total }}</strong><button v-if="group.total" class="ghost" @click="store.openWorkspace(group.category, null, {q: keyword.trim()}); store.paletteOpen = false">查看全部 →</button></header>
            <button v-for="row in group.rows" :key="row.id" class="search-hit" :class="{ active: active === indexOf(group.category, row) }"
              @mouseenter="active = indexOf(group.category, row)" @click="choose({category:group.category, row})">
              <span class="type-label">{{ row.kind }}</span><span><strong>{{ row.title || row.target_display || row.display_key || row.entity_key }}</strong><small v-if="row.text">{{ row.text }}</small></span><span>↗</span>
            </button>
            <p v-if="!group.total" class="dim none">没有匹配结果</p>
            <small v-else-if="group.next_cursor" class="dim">先显示 {{ group.rows.length }} 条，可查看全部并翻页。</small>
          </section>
        </template>
      </div>
      <footer class="dim">{{ data?.scope || '当前项目统一检索；不搜索原始文件正文。' }}<br />↑ ↓ 选择 · Enter 打开 · Esc 关闭</footer>
    </section>
  </div>
</template>

<style scoped>
.scrim { position: fixed; inset: 0; z-index: 40; background: var(--scrim); display:flex; align-items:flex-start; justify-content:center; padding: 7vh 16px; }
.palette { width: min(720px, 100%); max-height: 86vh; display:flex; flex-direction:column; padding: 16px; box-shadow: var(--raise); }
header { display:flex; align-items:center; justify-content:space-between; gap: 10px; margin-bottom: 10px; }
.results { overflow:auto; min-height: 60px; padding: 10px 0; }
.search-group { border-bottom:1px solid var(--line); padding: 10px 0; }
.search-hit { display:flex; align-items:center; gap:10px; width:100%; text-align:left; border:0; background:transparent; padding:10px; }
.search-hit.active { background:var(--tint-active); }
.search-hit > span:nth-child(2) { flex:1; min-width:0; }
.search-hit strong,.search-hit small { display:block; white-space:nowrap; text-overflow:ellipsis; overflow:hidden; }
.search-hit small { margin-top:5px; opacity:.7; }
.none,footer { font-size:12px; line-height:1.7; }
footer { margin-top:8px; }
</style>
