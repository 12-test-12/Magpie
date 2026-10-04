<script setup>
/**
 * 项目管理对话框（0.20.0 · 计划 F，形态参考 Yakit 的项目管理）。
 *
 * 一张带状态的清单 + 每行自己的动作，而不是把建 / 切 / 归档 / 删除四个动作
 * 摊到顶栏 —— 四个长得一样的按钮，每个都要人先想"这是不是我要的那个"。
 * 下拉只管"切"这一个高频动作，其余都收进这里。
 *
 * 三件刻意做的事：
 *
 *   1. **清单带归档**（`GET /projects?include_archived=1`）：归档项目从顶栏
 *      下拉里消失了，但恢复它要先看得见它 —— 这个对话框是唯一"看得见全部"
 *      的地方。归档行**不给「切换」**：切进去后顶栏下拉（活跃清单）里没有
 *      它，选中框会显示成一段空白 —— 要看归档项目，先恢复。
 *   2. **删除要逐字输入项目名**（confirm 由 store 送进 `DELETE` 查询串）：
 *      手滑的 DELETE 指向的是一整个评估的数据。行内确认，不弹第二层对话框
 *      —— 确认框套确认框是最容易点错的形状。
 *   3. **重建 = 新建 → 导入 → 归档/删除旧项目**（底部一行提示）：数据按项目
 *      隔离，迁移旧项目没有"搬迁"这一步，新旧并存互不影响。
 */
import { onMounted, ref } from 'vue'
import { get } from '../api.js'
import { store } from '../store.js'
import { fmtInt } from '../format.js'

const emit = defineEmits(['close'])

const rows = ref([])
const error = ref('')
//: 正在忙的行 id，或 'new'（新建）。同一时间只动一行 —— 两行同时提交，
//: 失败提示会落在错的行上。
const busy = ref('')
const newName = ref('')
const confirmId = ref(null)      // 正在输确认名的行
const confirmName = ref('')

async function load() {
  error.value = ''
  try {
    rows.value = await get('/projects?include_archived=1')
  } catch (err) {
    error.value = err.message
  }
}

onMounted(load)

async function create() {
  const name = newName.value.trim()
  if (!name) return
  busy.value = 'new'
  error.value = ''
  try {
    await store.createProject(name)
    await store.selectProject(name)   // 建完就切进去（建项目的动机就是用它）
    newName.value = ''
    await load()
    emit('close')
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = ''
  }
}

async function switchTo(row) {
  busy.value = row.id
  error.value = ''
  try {
    await store.selectProject(row.name)
    emit('close')
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = ''
  }
}

async function archive(row) {
  busy.value = row.id
  error.value = ''
  try {
    await store.archiveProject(row.id)   // 若归档的是当前项目，store 负责换指针
    await load()
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = ''
  }
}

async function restore(row) {
  busy.value = row.id
  error.value = ''
  try {
    await store.restoreProject(row.id)
    await load()
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = ''
  }
}

async function remove(row) {
  busy.value = row.id
  error.value = ''
  try {
    await store.deleteProject(row.id, confirmName.value)
    confirmId.value = null
    confirmName.value = ''
    await load()
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = ''
  }
}
</script>

<template>
  <div class="scrim" @click.self="emit('close')">
    <div class="dialog panel">
      <h1>项目管理</h1>
      <p class="dim small">
        数据按项目隔离：新建 / 导入 / 删除只影响单个项目，项目之间互不读写。
        归档把项目从日常工作清单里收起来（数据不动、可恢复）；删除是
        <b>不可恢复的硬删</b>，要输入项目名确认。
      </p>

      <div class="create">
        <input v-model="newName" placeholder="新项目名，例如 acme-2026q4"
               @keydown.enter="create" />
        <button class="primary" :disabled="busy === 'new' || !newName.trim()"
                @click="create">
          {{ busy === 'new' ? '建立中…' : '新建项目' }}
        </button>
      </div>

      <p v-if="error" class="bad small">{{ error }}</p>

      <table v-if="rows.length" class="list">
        <thead>
          <tr>
            <th>项目</th>
            <th class="num">实体</th>
            <th class="num">观测</th>
            <th>状态</th>
            <th class="right">动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.id"
              :class="{ archived: row.status === 'archived' }">
            <td>
              <span class="mono">{{ row.name }}</span>
              <span v-if="row.name === store.project" class="cur">当前</span>
            </td>
            <td class="num mono">{{ fmtInt(row.counts.entities) }}</td>
            <td class="num mono">{{ fmtInt(row.counts.observations) }}</td>
            <td>{{ row.status === 'archived' ? '已归档' : '活跃' }}</td>
            <td class="acts">
              <template v-if="confirmId === row.id">
                <input v-model="confirmName" class="confirm" autofocus
                       :placeholder="`输入 ${row.name} 以确认删除`"
                       @keydown.enter="confirmName === row.name && remove(row)" />
                <button class="primary"
                        :disabled="confirmName !== row.name || busy === row.id"
                        @click="remove(row)">
                  {{ busy === row.id ? '删除中…' : '确认删除' }}
                </button>
                <button class="ghost" @click="confirmId = null; confirmName = ''">
                  取消
                </button>
              </template>
              <template v-else>
                <button v-if="row.status !== 'archived' && row.name !== store.project"
                        class="ghost" :disabled="busy === row.id"
                        @click="switchTo(row)">切换</button>
                <button v-if="row.status !== 'archived'" class="ghost"
                        :disabled="busy === row.id" @click="archive(row)">归档</button>
                <button v-else class="ghost" :disabled="busy === row.id"
                        @click="restore(row)">恢复</button>
                <button class="ghost" title="不可恢复的硬删"
                        :disabled="busy === row.id"
                        @click="confirmId = row.id; confirmName = ''">删除</button>
              </template>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else class="dim small">还没有项目 —— 上面建一个。</p>

      <!-- 重建流程（计划 F.3 文档的一行版）：没有"搬迁"这一步，因为数据按
           项目隔离，新旧并存互不影响。 -->
      <p class="dim small hint">
        <b>重建项目</b>：新建项目 → 导入数据 → 归档或删除旧项目。数据按项目隔离，
        新旧并存互不影响，没有「搬迁」这一步。
      </p>

      <div class="foot">
        <span class="spacer" />
        <button class="ghost" @click="emit('close')">关闭</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.scrim {
  position: fixed; inset: 0; z-index: 50; background: var(--scrim);
  display: flex; align-items: center; justify-content: center; padding: 24px;
}
.dialog {
  width: 660px; max-height: 84vh; overflow: auto;
  padding: 18px 20px; background: var(--panel-2); box-shadow: var(--raise);
}
h1 { font-size: 16px; margin: 0 0 8px; }
.small { font-size: 12px; }
.dim { color: var(--dim); }
.create { display: flex; gap: 8px; margin: 12px 0 4px; }
.create input { flex: 1; width: auto; }
.list { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 10px; }
.list th {
  text-align: left; color: var(--faint); font-weight: 500;
  padding: 6px 8px; border-bottom: 1px solid var(--line);
}
.list td { padding: 7px 8px; border-bottom: 1px solid var(--line); }
.list .num { text-align: right; width: 76px; }
.list .right { text-align: right; }
.list tr.archived td { color: var(--dim); }
.cur {
  font-size: 10px; color: var(--accent); border: 1px solid var(--accent);
  border-radius: 999px; padding: 0 6px; margin-left: 6px;
}
.acts { text-align: right; white-space: nowrap; }
.acts button { margin-left: 6px; }
.acts input.confirm {
  display: inline-block; width: 190px; padding: 3px 7px;
  font-size: 11px; margin-left: 6px;
}
.hint { margin-top: 12px; line-height: 1.7; }
.foot { display: flex; margin-top: 14px; }
.spacer { flex: 1; }
</style>
