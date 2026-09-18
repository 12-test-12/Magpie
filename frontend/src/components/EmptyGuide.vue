<script setup>
/**
 * 空状态引导卡（计划书 §7.4 最后一条，也是 2.8 交付标准表里的一行）。
 *
 * 计划书的原话是「**首次打开的空状态是这个 App 的门面**：一张"从这里开始"的
 * 引导卡，而不是空白表格」。这句话值得照做：空库时用户看到的不是"没有数据"，
 * 而是"这个工具会怎么被用起来"。空白表格传达的信息量是零，甚至更糟 ——
 * 它让人怀疑是不是加载失败了。
 *
 * 三种空状态要分开处理，因为**下一步该做什么**完全不同：
 *
 *   * 一个项目都没有 → 先建项目（`POST /projects`，2.7 就有的端点）
 *   * 有项目但库是空的 → 去导入（`POST /imports` + SSE）
 *   * 有实体但当前筛选没匹配上 → 是筛选的问题，不是数据的问题
 *
 * 把它们都渲染成同一句"暂无数据"，等于把三种不同的处境说成同一种。
 */
import { computed, ref } from 'vue'
import { store } from '../store.js'

const emit = defineEmits(['import'])

const name = ref('')
const busy = ref(false)
const error = ref('')

const hasProjects = computed(() => store.projects.length > 0)
const hasEntities = computed(() => store.counts.entities > 0)
const filtered = computed(() => store.filter.kinds.length > 0 || Boolean(store.filter.keyword))

async function createProject() {
  const value = name.value.trim()
  if (!value) {
    error.value = '先给项目起个名字。'
    return
  }
  busy.value = true
  error.value = ''
  try {
    const created = await store.createProject(value)
    await store.selectProject(created.name)
    name.value = ''
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="guide panel">
    <template v-if="!hasProjects">
      <h1>从这里开始</h1>
      <p class="dim">
        Magpie 不主动扫描任何东西 —— 它把你已有的工具输出（subfinder / httpx…）
        收进来，整理成一张能看的图。**打开画布就能看见手上有什么、它们怎么连起来。**
      </p>
      <ol class="steps">
        <li><b>建一个项目</b>：项目 = 一次评估的范围，数据按项目隔离。</li>
        <li><b>导入文件</b>：`subfinder.txt` / `httpx.jsonl`，一次可以选多个。</li>
        <li><b>回画布</b>：`domain → ip`、`service → product` 会连起来。</li>
      </ol>
      <div class="row">
        <input v-model="name" placeholder="项目名，例如 acme-2026q3"
               @keydown.enter="createProject" />
        <button class="primary" :disabled="busy" @click="createProject">
          {{ busy ? '建立中…' : '建项目' }}
        </button>
      </div>
      <p v-if="error" class="bad small">{{ error }}</p>
    </template>

    <template v-else-if="!hasEntities">
      <h1>项目建好了，还没有数据</h1>
      <p class="dim">
        导入一份真实输出就能看到画布动起来。路径填**绝对路径**（服务进程按它
        自己的工作目录解析相对路径）。导入会在底部日志抽屉里实时显示进度、阶段
        与坏行 —— 那些是数据层的真实计数，不是界面上估的。
      </p>
      <ol class="steps">
        <li>`subfinder -d example.com -o subfinder.txt`</li>
        <li>`httpx -json -l subfinder.txt -o httpx.jsonl`</li>
        <li>把这两个文件一起导进来</li>
      </ol>
      <button class="primary" @click="emit('import')">开始导入</button>
    </template>

    <template v-else>
      <h1>当前筛选没有匹配的实体</h1>
      <p class="dim">
        库里有 {{ store.counts.entities }} 个实体，只是被筛掉了。
      </p>
      <button class="ghost" @click="store.clearFilter()">
        清除筛选{{ filtered ? '' : '（本来就没有）' }}
      </button>
    </template>
  </div>
</template>

<style scoped>
.guide {
  max-width: 520px;
  padding: 22px 24px;
  text-align: left;
  background: var(--panel-2);
}
h1 { font-size: 17px; margin: 0 0 10px; font-weight: 600; }
.guide p { margin: 0 0 12px; font-size: 12px; line-height: 1.7; }
.steps { margin: 0 0 14px; padding-left: 20px; font-size: 12px; line-height: 1.9; }
.steps b { color: var(--accent); font-weight: 600; }
.row { display: flex; gap: 8px; }
.row input { flex: 1; }
.small { font-size: 11px; }
button + button { margin-left: 8px; }
</style>
