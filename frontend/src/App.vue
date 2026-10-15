<script setup>
/**
 * Research workspace shell: business navigation and bounded master/detail views.
 * Canvas and pipeline retain their existing contextual panels and contracts.
 * Boot loads projects/meta/lens counts; graph rows are fetched on explicit entry.
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import TopBar from './components/TopBar.vue'
import LeftPanel from './components/LeftPanel.vue'
import WorkspaceNav from './components/WorkspaceNav.vue'
import WorkspaceView from './views/WorkspaceView.vue'
import { workspacePages } from './workspace.js'
import DetailPanel from './components/DetailPanel.vue'
import LogDrawer from './components/LogDrawer.vue'
import CommandPalette from './components/CommandPalette.vue'
import GraphCanvas from './components/GraphCanvas.vue'
import EmptyGuide from './components/EmptyGuide.vue'
import LensView from './views/LensView.vue'
import PipelineView from './views/PipelineView.vue'
import JobsView from './views/JobsView.vue'
import InboxView from './views/InboxView.vue'
import DoctorView from './views/DoctorView.vue'
import McpView from './views/McpView.vue'
import RiskView from './views/RiskView.vue'
import OverviewView from './views/OverviewView.vue'
import ImportDialog from './views/ImportDialog.vue'
import ProjectManager from './components/ProjectManager.vue'
import { store } from './store.js'
import { hasToken } from './api.js'

const importOpen = ref(false)
const manageOpen = ref(false)
const booted = ref(false)
const fatal = ref('')

const showDetail = computed(() => store.view === 'explore')

//: 镜头页**动态接线**：`meta.kinds[].lens` 登记了 6 个镜头（assets / services /
//: vuln / api / traffic / graph），旧版只写死了 3 个分支 —— 左栏点「接口」
//: 「流量」会 setView 成一个没有任何分支认领的 key，中间直接空白。LensView
//: 本来就按 `lens` prop 通用（FACETS 按 lens 查表），所以按 `lensKeys` 认领
//: 即可：登记一个新镜头，页面自己出现 —— 与 `loadLenses` 同一条纪律。
const activeLensLabel = computed(() => {
  const row = (store.lensRows || []).find((entry) => entry.key === store.view)
  return row ? row.label : ''
})

async function boot() {
  try {
    await store.reload()
    booted.value = true
  } catch (err) {
    fatal.value = err.message
  }
}

function onKey(event) {
  const meta = event.ctrlKey || event.metaKey
  if (meta && (event.key === 'k' || event.key === 'K')) {
    event.preventDefault()
    store.paletteOpen = !store.paletteOpen
  } else if (event.key === 'Escape') {
    store.paletteOpen = false
  }
}

onMounted(() => {
  window.addEventListener('keydown', onKey)
  if (hasToken()) boot()
})
onUnmounted(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div class="app">
    <TopBar
      :version="store.meta ? 'v' + store.meta.magpie_version : ''"
      @import="importOpen = true"
      @palette="store.paletteOpen = true"
      @manage="manageOpen = true"
    />

    <div v-if="!hasToken()" class="token-missing">
      <div class="panel card">
        <h1>URL 里没有 token</h1>
        <p class="dim">
          请用 `magpie app` 打印出来的那条 URL 打开本页
          （形如 <span class="mono">http://127.0.0.1:&lt;端口&gt;/?token=&lt;一次性 token&gt;</span>）。
          token 是启动时随机生成的，刷新页面不会丢，但重启服务会换一个。
        </p>
      </div>
    </div>

    <div v-else-if="fatal" class="token-missing">
      <div class="panel card">
        <h1 class="bad">连不上服务</h1>
        <p class="mono small">{{ fatal }}</p>
        <p class="dim small">
          服务只监听回环地址（127.0.0.1）。如果它已经停了，重新跑一次
          `magpie app`。
        </p>
      </div>
    </div>

    <main v-else>
      <LeftPanel v-if="['explore', 'pipeline'].includes(store.view)" />
      <WorkspaceNav v-else />

      <section class="center">
        <!-- 项目总览（评审十一）= 默认首页。空库时它内部渲染 EmptyGuide ——
             "从这里开始 / 还没有数据"两张引导卡从此长在首页上，而不是藏在
             一个要先点进去的画布里。 -->
        <OverviewView v-if="store.view === 'overview'" @import="importOpen = true" />
        <template v-else-if="store.view === 'explore'">
          <GraphCanvas>
            <template #empty>
              <EmptyGuide @import="importOpen = true" />
            </template>
          </GraphCanvas>
        </template>
        <!-- 编排模式（`store.view` 的第二个取值）。**可编辑只在这一支里**：
             `GraphCanvas.vue` 那一边的只读保证一个字没动。右栏详情
             （`showDetail`）只跟着 `explore` 出现，所以这里不用改它 ——
             它已经是对的。 -->
        <PipelineView v-else-if="store.view === 'pipeline'" />
        <WorkspaceView v-else-if="workspacePages[store.view]" :category="store.view" @import="importOpen = true" />
        <!-- 镜头页按 `lensKeys` 动态认领（见 activeLensLabel 的注释）。 -->
        <LensView v-else-if="store.lensKeys.includes(store.view)"
                  :lens="store.view" :label="activeLensLabel"
                  @import="importOpen = true" />
        <JobsView v-else-if="store.view === 'jobs'" @import="importOpen = true" />
        <InboxView v-else-if="store.view === 'inbox'" />
        <RiskView v-else-if="store.view === 'risk'" />
        <DoctorView v-else-if="store.view === 'doctor'" />
        <McpView v-else-if="store.view === 'mcp'" />
      </section>

      <DetailPanel v-if="showDetail" />
    </main>

    <LogDrawer />
    <CommandPalette />
    <ImportDialog v-if="importOpen" @close="importOpen = false" />
    <ProjectManager v-if="manageOpen" @close="manageOpen = false" />

    <div v-if="!booted && !fatal" class="booting dim">载入中…</div>
  </div>
</template>

<style scoped>
.app { display: flex; flex-direction: column; height: 100%; }
main { flex: 1; display: flex; min-height: 0; }
.center { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.token-missing {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.card { max-width: 520px; padding: 20px 22px; background: var(--panel-2); }
.card h1 { font-size: 16px; margin: 0 0 10px; }
.card p { margin: 0 0 10px; font-size: 12px; line-height: 1.7; }
.small { font-size: 12px; }
.booting { padding: 8px 14px; font-size: 12px; }
</style>
