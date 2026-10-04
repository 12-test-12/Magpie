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
import { fmtInt } from '../format.js'
import EmptyState from './EmptyState.vue'

const emit = defineEmits(['import'])

const name = ref('')
const busy = ref(false)
const error = ref('')

const hasProjects = computed(() => store.projects.length > 0)
//: "库里有没有实体"看的是**项目总数**（`/projects` 的 counts），不是已载入数
//: （0.20.0）—— kind 筛选推到服务端之后，筛一个库里没有的类型会返回 0 个节点，
//: 拿已载入数判断会把"筛选没匹配"错报成"项目还没数据"（两个下一步完全不同：
//: 一个是清除筛选，一个是去导入）。
const hasEntities = computed(() => store.projectTotal > 0 || store.counts.entities > 0)
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
  <!-- 外壳换成 EmptyState（插画 + 层级 + CTA），三分支逻辑与全部
       钉死文案（从这里开始 / 建项目 / 还没有数据 / 开始导入）原样保留。 -->
  <div class="guide panel">
    <template v-if="!hasProjects">
      <EmptyState
        title="从这里开始"
        desc="Magpie 不主动扫描任何东西 —— 它把你已有的工具输出（subfinder / httpx…）收进来，整理成一张能看的图。打开画布就能看见手上有什么、它们怎么连起来。"
      >
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
      </EmptyState>
    </template>

    <template v-else-if="!hasEntities">
      <EmptyState
        title="项目建好了，还没有数据"
        desc="导入一份真实输出就能看到画布动起来。路径填绝对路径（服务进程按它自己的工作目录解析相对路径）。导入会在底部日志抽屉里实时显示进度、阶段与坏行 —— 那些是数据层的真实计数，不是界面上估的。"
      >
        <ol class="steps">
          <li>`subfinder -d example.com -o subfinder.txt`</li>
          <li>`httpx -json -l subfinder.txt -o httpx.jsonl`</li>
          <li>把这两个文件一起导进来</li>
        </ol>
        <button class="primary" @click="emit('import')">开始导入</button>
      </EmptyState>
    </template>

    <template v-else>
      <!-- 三个口径并排（0.20.0）：库中共 / 符合当前筛选 / 画布已载入 —— 原来
           只写"库里有 500 个实体"，500 是载入数（/graph 的 limit），不是库。
           tone 用 compact（GPT ①）：筛选没匹配是中性状态，不是"干净"的好消息
           （大绿勾）—— 小图标 + 收窄留白就够。 -->
      <EmptyState
        title="当前筛选没有匹配的实体"
        :desc="`库中共 ${fmtInt(store.projectTotal)} 个实体；符合当前筛选的 ` +
               `${fmtInt(store.graphTotal)} 个，画布已加载 ` +
               `${fmtInt(store.counts.entities)} 个。`"
        tone="compact"
      >
        <button class="ghost" @click="store.clearFilter()">
          清除筛选{{ filtered ? '' : '（本来就没有）' }}
        </button>
      </EmptyState>
    </template>
  </div>
</template>

<style scoped>
.guide {
  max-width: 540px;
  background: var(--panel-2);
  border: 1px solid var(--line);
  box-shadow: var(--raise);
}
/* 步骤与表单是 EmptyState actions 插槽里的内容：居中排版改成左对齐宽块，
   保证输入框有足够宽度、步骤文字不被 460px 的居中列挤断。 */
.guide :deep(.empty-state) { max-width: 100%; padding: 26px 28px; }
/* 插槽里是多段块级内容（步骤 ol + 表单行），纵向排 */
.guide :deep(.actions) { flex-direction: column; align-items: stretch; width: 100%; }
.steps {
  margin: 0;
  padding-left: 20px;
  font-size: 12px;
  line-height: 1.9;
  text-align: left;
  color: var(--dim);
}
.steps b { color: var(--accent); font-weight: 600; }
.row { display: flex; gap: 8px; margin-top: 12px; }
.row input { flex: 1; }
.small { font-size: 12px; margin: 8px 0 0; }
</style>
