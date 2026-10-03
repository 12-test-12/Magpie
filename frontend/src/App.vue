<script setup>
/**
 * n8n 式外壳（计划书 §7.1）：
 *
 *   顶部 项目切换 + 导入 + Ctrl+K + 体检 + Inbox 徽标
 *   左栏 类型面板 + 镜头切换
 *   中间 画布 / 镜头页 / 作业历史 / Inbox / 体检
 *   右栏 实体详情（只在画布页）
 *   底部 日志抽屉
 *
 * 首屏的请求是 `projects → meta → (graph ∥ lenses)`：镜头清单由
 * `meta.kinds[].lens` 推导，所以它必须排在 `lenses` 前面（3.2 之前的两者并行
 * 是"镜头写死在代码里"才成立的前提）。除此之外没有瀑布 —— 多一层就多一个
 * RTT，而这是"首屏 < 1.5s"这条契约里**界面能控制的那一半**；另一半是服务端
 * 与浏览器的，见 `bench/test_perf_web.py` 里对口径的说明。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import TopBar from './components/TopBar.vue'
import LeftPanel from './components/LeftPanel.vue'
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
import RiskView from './views/RiskView.vue'
import ImportDialog from './views/ImportDialog.vue'
import { store } from './store.js'
import { hasToken } from './api.js'

const importOpen = ref(false)
const booted = ref(false)
const fatal = ref('')

const showDetail = computed(() => store.view === 'explore')

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
      <LeftPanel />

      <section class="center">
        <template v-if="store.view === 'explore'">
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
        <LensView v-else-if="store.view === 'assets'" lens="assets" label="资产"
                  @import="importOpen = true" />
        <LensView v-else-if="store.view === 'services'" lens="services"
                  label="服务与中间件" @import="importOpen = true" />
        <LensView v-else-if="store.view === 'vuln'" lens="vuln" label="漏洞"
                  @import="importOpen = true" />
        <JobsView v-else-if="store.view === 'jobs'" @import="importOpen = true" />
        <InboxView v-else-if="store.view === 'inbox'" />
        <RiskView v-else-if="store.view === 'risk'" />
        <DoctorView v-else-if="store.view === 'doctor'" />
      </section>

      <DetailPanel v-if="showDetail" />
    </main>

    <LogDrawer />
    <CommandPalette />
    <ImportDialog v-if="importOpen" @close="importOpen = false" />

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
