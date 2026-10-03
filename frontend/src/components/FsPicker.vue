<script setup>
/**
 * 目录浏览框 —— 导入对话框里的「浏览…」。
 *
 * 为什么不是浏览器原生的「选择文件夹」：**网页拿不到绝对路径**（安全模型
 * 如此，`webkitdirectory` / `showDirectoryPicker` 只给相对路径与文件内容），
 * 而 `POST /imports` 要的是服务进程能解析的绝对路径。所以由服务端列目录
 * （`GET /api/v1/fs`），这里只负责点选 —— 浏览时看到的路径与提交后读到的
 * 是同一个进程给出的同一条，不会各说各话。
 *
 * 交互按"走"来设计：
 *
 *   - 点目录 = 进去，点文件 = 直接选中（选中即关闭，路径回到导入表单）；
 *   - 「选择此文件夹」选**当前**所在目录（根视图没有"当前目录"，按钮禁用）；
 *   - 「上一级」到 `parent`，到顶（`parent` 为 null）时禁用。
 */
import { onMounted, ref } from 'vue'
import { get } from '../api.js'

const emit = defineEmits(['pick', 'close'])

const path = ref('')          // '' = 根视图（Windows 盘符 / POSIX /）
const parent = ref(null)
const entries = ref([])
const error = ref('')
const busy = ref(false)

async function load(target) {
  busy.value = true
  error.value = ''
  try {
    const data = await get('/fs' +
      (target ? `?path=${encodeURIComponent(target)}` : ''))
    path.value = data.path
    parent.value = data.parent
    entries.value = data.entries || []
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = false
  }
}

/** 点一行：目录就进去，文件就选中带走。 */
function activate(entry) {
  if (entry.is_dir) {
    load(entry.path)
  } else {
    emit('pick', entry.path)
  }
}

function chooseCurrent() {
  if (path.value) emit('pick', path.value)
}

onMounted(() => load(''))
</script>

<template>
  <div class="scrim" @click.self="emit('close')">
    <div class="dialog panel">
      <h1>选择文件或文件夹</h1>
      <p class="dim small">
        走服务端的目录树 —— 路径是**绝对路径**，点文件直接填回导入表单，
        点目录是「进去」。
      </p>

      <div class="bar">
        <button class="ghost" :disabled="!parent || busy" @click="load(parent)">
          上一级
        </button>
        <span class="where mono">{{ path || '（根）' }}</span>
        <span class="spacer" />
        <button class="ghost" :disabled="!path" @click="emit('close')">取消</button>
        <button class="primary" :disabled="!path" @click="chooseCurrent">
          选择此文件夹
        </button>
      </div>

      <p v-if="error" class="bad small">{{ error }}</p>

      <ul class="entries mono">
        <li v-if="busy" class="dim">读取中…</li>
        <li v-else-if="!entries.length" class="dim">（空目录）</li>
        <li v-for="entry in entries" :key="entry.path">
          <button type="button" class="row" @click="activate(entry)">
            <span class="kind">{{ entry.is_dir ? '目录' : '文件' }}</span>
            <span class="name">{{ entry.name }}</span>
          </button>
        </li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.scrim { position: fixed; inset: 0; z-index: 60; background: var(--scrim);
         display: flex; align-items: center; justify-content: center; padding: 24px; }
.dialog { width: 560px; padding: 18px 20px; background: var(--panel-2);
          box-shadow: var(--raise); }
h1 { font-size: 16px; margin: 0 0 8px; }
.bar { display: flex; align-items: center; gap: 8px; margin: 10px 0 8px; }
.where { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis;
         white-space: nowrap; font-size: 11px; color: var(--dim); }
.spacer { flex: 0 0 4px; }
.small { font-size: 11px; line-height: 1.6; }
.entries { list-style: none; margin: 8px 0 0; padding: 6px; max-height: 300px;
           overflow-y: auto; background: var(--bg); border: 1px solid var(--line);
           border-radius: 6px; font-size: 12px; }
.entries li + li { margin-top: 2px; }
.row { display: flex; gap: 10px; width: 100%; padding: 4px 8px; text-align: left;
       background: none; border: 0; border-radius: 4px; cursor: pointer;
       color: var(--dim); font: inherit; }
.row:hover { background: var(--tint-hover); color: var(--fg); }
.kind { flex: 0 0 3em; color: var(--faint); font-size: 11px; }
.name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
