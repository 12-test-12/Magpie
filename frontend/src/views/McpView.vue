<script setup>
/**
 * MCP 接入页（0.21.0）—— 配置 + 工具清单 + 页内试用，三张卡各答一个问题：
 *
 *   **怎么接？** `GET /api/v1/mcp` 给出的 `config.mcpServers` 是 Claude
 *   Desktop 等客户端的接线片段（`--db` 是绝对路径 —— 客户端按自己的 cwd 解析，
 *   相对接不上）。复制按钮成败可见（与 DetailPanel 的 copyObs 同款纪律：点了
 *   没反应会被当成复制成功）。**不重构 DetailPanel 的剪贴板 helper** ——
 *   verify_g5 B4a 审计着那个文件的形状，这边抄一份纪律、不动它。
 *
 *   **能干什么？** 工具清单由后端现算（`McpServer.tools/list` 同源），只读 /
 *   写两色徽章来自 `readonly_tools` / `write_tools` —— 页面上没有第二份清单，
 *   适配器加了工具这页自动跟着变。写面只有 `add_finding`（AI 发现通道，
 *   `jobs.kind="ai_note"`，可撤销）—— **非必要不修改**就体现在这个 1 上。
 *
 *   **真的通吗？** 页内试用走 **HTTP 同义端点**（不走 stdio 协议 —— 浏览器
 *   起不了子进程），映射表就写在下面的 `EQUIV`。没有 HTTP 等价的
 *   （schema / timeline）如实标出来，不硬凑；`diff` 的 501 信封原样显示 ——
 *   成功与失败一视同仁地展示，试用面板才不是个只会报喜的玩具。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { get, post, ApiError } from '../api.js'
import { store } from '../store.js'

const info = ref(null)
const loading = ref(false)
const error = ref('')
const copyState = ref(null)     // {text, bad} —— 复制的成败都必须看得见

// ---------------------------------------------------------------------------
// 试用：工具名 → HTTP 同义端点（本地映射表，唯一一份）
// ---------------------------------------------------------------------------
//
// `http: null` = 没有 HTTP 等价（页面如实标注，不假装能试）。`needs` 是本地
// 先行校验（ref 必须是整数 —— HTTP 这层没有 CLI/MCP 的键归一化，给了域名会
// 当成 404，本地说清楚比让 404 冤枉人好）。
const EQUIV = {
  ls: { http: 'GET /entities' },
  lens: { http: 'GET /lenses/{lens}' },
  show: { http: 'GET /entities/{ref}', needs: ['int-ref'] },
  doctor: { http: 'GET /doctor' },
  jobs: { http: 'GET /jobs 或 /jobs/{id}' },
  risk: { http: 'GET /risk' },
  agent_context: { http: 'GET /agent/context' },
  diff: { http: 'GET /diff（有界摘要；完整事实明细仍使用 CLI / MCP）' },
  add_finding: { http: 'POST /notes', needs: ['int-ref'], write: true },
  schema: { http: null, note: '仅 CLI / MCP，无 HTTP 等价' },
  timeline: { http: null, note: '仅 CLI / MCP，无 HTTP 等价' },
}

const selected = ref('ls')
const args = ref({})
const trialResult = ref(null)   // 展示用的信封：{ok:true,data} 或 {ok:false,error}
const trialLocal = ref('')      // 本地提示（没有 HTTP 等价 / ref 不是整数…）
const trialBusy = ref(false)

const tools = computed(() => (info.value && info.value.tools) || [])
const selectedTool = computed(() =>
  tools.value.find((t) => t.name === selected.value) || null)
const readonlyCount = computed(() =>
  (info.value && info.value.readonly_tools ? info.value.readonly_tools.length : 0))
const writeCount = computed(() =>
  (info.value && info.value.write_tools ? info.value.write_tools.length : 0))

const configText = computed(() =>
  info.value ? JSON.stringify(info.value.config, null, 2) : '')

const propRows = computed(() => {
  const schema = (selectedTool.value && selectedTool.value.inputSchema) || {}
  const props = schema.properties || {}
  const required = new Set(schema.required || [])
  return Object.entries(props).map(([name, spec]) => ({
    name, spec, required: required.has(name),
    label: EQUIV[selected.value] && EQUIV[selected.value].write && name === 'ref'
      ? 'entity_id（整数）' : name,
  }))
})

/** 选中工具变化 / 首次载入：按 inputSchema 重置表单（default 优先，enum 留空待选）。 */
function resetArgs() {
  const schema = (selectedTool.value && selectedTool.value.inputSchema) || {}
  const props = schema.properties || {}
  const next = {}
  for (const [name, spec] of Object.entries(props)) {
    if (spec.type === 'boolean') next[name] = Boolean(spec.default)
    else if (spec.default !== undefined && spec.default !== null) next[name] = spec.default
    else next[name] = ''
  }
  args.value = next
  trialResult.value = null
  trialLocal.value = ''
}

function pick(name) {
  selected.value = name
  resetArgs()
}

// --- 请求构造：参数 → 查询串 / JSON 体（跳过空值，project 缺省用当前项目） ---

function cleanArgs(skip) {
  const out = {}
  for (const [k, v] of Object.entries(args.value)) {
    if (skip.includes(k)) continue
    if (v === '' || v === null || v === undefined) continue
    if (typeof v === 'boolean' && !v) continue
    out[k] = v
  }
  return out
}

function queryString(clean, extra) {
  const all = Object.assign({}, clean, extra || {})
  const project = all.project || store.project || ''
  delete all.project
  const q = new URLSearchParams()
  for (const [k, v] of Object.entries(all)) q.set(k, String(v))
  if (project) q.set('project', String(project))
  const s = q.toString()
  return s ? `?${s}` : ''
}

function buildCall(name) {
  const equiv = EQUIV[name] || {}
  if (!equiv.http) {
    return { local: equiv.note || '这个工具没有 HTTP 等价端点，页内试用不了' }
  }
  const needs = equiv.needs || []
  if (needs.includes('int-ref')) {
    const ref = String(args.value.ref === undefined ? '' : args.value.ref).trim()
    if (!/^\d+$/.test(ref)) {
      return { local: 'ref 必须是整数 entity_id —— HTTP 层没有键归一化（域名 / '
        + 'key 请走 CLI 或 MCP；id 可在实体详情或 GET /entities 里查）' }
    }
  }
  switch (name) {
    case 'ls':
    case 'risk':
    case 'doctor':
    case 'diff':
      return { method: 'GET', path: `/${name === 'ls' ? 'entities' : name}`
        + queryString(cleanArgs([])) }
    case 'lens': {
      const lens = String(args.value.lens || '').trim()
      if (!lens) return { local: 'lens 是必填 —— 先选一个镜头' }
      return { method: 'GET',
        path: `/lenses/${encodeURIComponent(lens)}`
          + queryString(cleanArgs(['lens'])) }
    }
    case 'show': {
      const ref = String(args.value.ref || '').trim()
      return { method: 'GET',
        path: `/entities/${encodeURIComponent(ref)}`
          + queryString(cleanArgs(['ref'])) }
    }
    case 'jobs': {
      const job = String(args.value.job || '').trim()
      const qs = queryString(cleanArgs(['job']))
      return { method: 'GET',
        path: job ? `/jobs/${encodeURIComponent(job)}${qs}` : `/jobs${qs}` }
    }
    case 'agent_context':
      return { method: 'GET',
        path: '/agent/context' + queryString(cleanArgs([])) }
    case 'add_finding': {
      const body = {
        entity_id: Number(String(args.value.ref).trim()),
        text: String(args.value.text === undefined ? '' : args.value.text),
        dry_run: Boolean(args.value.dry_run),
      }
      return { method: 'POST', path: '/notes', body,
        query: queryString(cleanArgs(['ref', 'text', 'dry_run'])) }
    }
    default:
      return { local: '这个工具还没接上页内试用（映射表 EQUIV 里补一行）' }
  }
}

async function runTrial() {
  trialLocal.value = ''
  trialResult.value = null
  const call = buildCall(selected.value)
  if (call.local) {
    trialLocal.value = call.local
    return
  }
  trialBusy.value = true
  try {
    const path = call.path + (call.query || '')
    const data = call.method === 'POST' ? await post(path, call.body)
      : await get(path)
    trialResult.value = { ok: true, data }
  } catch (err) {
    // 成功与失败一视同仁地展示：ApiError 自带 code/hint（信封的 error 半边），
    // 其它异常（网络 / 程序缺陷）也如实显示，不吞。
    if (err instanceof ApiError) {
      trialResult.value = { ok: false, status: err.status,
        error: { code: err.code, message: err.message.split(': ').slice(1).join(': ')
          || err.message, hint: err.hint } }
    } else {
      trialResult.value = { ok: false, error: { code: 'E_CLIENT', message: String(err) } }
    }
  } finally {
    trialBusy.value = false
  }
}

const trialJson = computed(() =>
  trialResult.value ? JSON.stringify(trialResult.value, null, 2) : '')

// --- 复制配置（成败可见；127.0.0.1 是安全上下文，无需 fallback） -------------

async function copyConfig() {
  const clipboard = typeof navigator === 'undefined' ? null : navigator.clipboard
  if (!clipboard || typeof clipboard.writeText !== 'function') {
    copyState.value = { text: '没有剪贴板 API', bad: true }
    return
  }
  try {
    await clipboard.writeText(configText.value)
    copyState.value = { text: '已复制', bad: false }
  } catch (err) {
    copyState.value = { text: `复制失败：${err.message || err}`, bad: true }
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    info.value = await get('/mcp')
    if (!EQUIV[selected.value]) selected.value = 'ls'
    resetArgs()
  } catch (err) {
    error.value = err.message
    info.value = null
  } finally {
    loading.value = false
  }
}

onMounted(() => { load() })
watch(() => store.view, (value) => { if (value === 'mcp') load() })
watch(selected, resetArgs)
</script>

<template>
  <div class="mcp">
    <header class="head">
      <h2 class="section">MCP 接入</h2>
      <span v-if="info" class="chip">{{ readonlyCount }} 个只读工具</span>
      <span v-if="info" class="chip" :class="writeCount ? 'warn' : ''">
        {{ writeCount }} 个写通道
      </span>
      <span class="spacer" />
      <button class="ghost" :disabled="loading" @click="load">刷新</button>
    </header>

    <p v-if="error" class="bad small">{{ error }}</p>
    <p v-else-if="loading && !info" class="dim">读取接入信息…</p>

    <div v-if="info" class="cols">
      <!-- ① 接入配置 -->
      <section class="panel card wide">
        <h2 class="section">接入配置</h2>
        <p class="faint small">
          粘进 Claude Desktop 等 MCP 客户端的 <code>mcpServers</code>。
          <code>--db</code> 是绝对路径（客户端按自己的 cwd 解析，接线别写相对）。
          stdio 传输、零网络 —— 服务端不发起任何出站请求。
        </p>
        <div class="copyrow">
          <button class="ghost" @click="copyConfig">复制配置</button>
          <span v-if="copyState" :class="copyState.bad ? 'bad' : 'ok'" class="small">
            {{ copyState.text }}</span>
        </div>
        <pre class="config mono">{{ configText }}</pre>
        <dl class="kv">
          <dt>服务</dt>
          <dd>{{ info.server.name }} v{{ info.server.version }}
            （协议 {{ info.server.protocol_version }}）</dd>
          <dt>说明</dt><dd class="faint">{{ info.server.instructions }}</dd>
        </dl>
      </section>

      <!-- ② 工具清单 -->
      <section class="panel card wide">
        <h2 class="section">工具清单</h2>
        <p class="faint small">
          由适配器 <code>tools/list</code> 现算 —— 这页与客户端看到的是同一份，
          适配器加了工具这里自动跟着变。点一行把它装进下面的试用面板。
        </p>
        <table class="tools">
          <thead>
            <tr><th>工具</th><th>说明</th><th>必填</th><th>类别</th></tr>
          </thead>
          <tbody>
            <tr v-for="tool in tools" :key="tool.name"
                :class="tool.name === selected ? 'on' : ''"
                @click="pick(tool.name)">
              <td class="mono">{{ tool.name }}</td>
              <td class="desc">{{ tool.description }}</td>
              <td class="mono faint">
                {{ (tool.inputSchema.required || []).join(', ') || '—' }}</td>
              <td>
                <span v-if="(info.write_tools || []).includes(tool.name)"
                      class="chip warn">写通道 · AI 发现</span>
                <span v-else class="chip">只读</span>
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <!-- ③ 页内试用 -->
      <section class="panel card wide">
        <h2 class="section">页内试用</h2>
        <p class="faint small">
          走 HTTP 同义端点（浏览器起不了 stdio 子进程），信封原样展示 ——
          成功与失败一视同仁。
        </p>

        <div class="trialrow">
          <label class="faint small">工具</label>
          <select v-model="selected" class="toolsel">
            <option v-for="tool in tools" :key="tool.name" :value="tool.name">
              {{ tool.name }}</option>
          </select>
          <span v-if="EQUIV[selected]" class="chip">{{ EQUIV[selected].http }}</span>
          <span v-else class="chip warn">无 HTTP 等价</span>
        </div>

        <div v-if="selectedTool" class="form">
          <div v-for="row in propRows" :key="row.name" class="field">
            <label :for="`mcp-${row.name}`" class="faint small">
              {{ row.label }}<b v-if="row.required" class="bad">*</b>
            </label>
            <select v-if="row.spec.enum" :id="`mcp-${row.name}`"
                    v-model="args[row.name]">
              <option value="" disabled>（选一个）</option>
              <option v-for="opt in row.spec.enum" :key="String(opt)"
                      :value="opt">{{ opt }}</option>
            </select>
            <input v-else-if="row.spec.type === 'boolean'"
                   :id="`mcp-${row.name}`" type="checkbox"
                   v-model="args[row.name]" />
            <input v-else-if="row.spec.type === 'integer'
                              || row.spec.type === 'number'"
                   :id="`mcp-${row.name}`" type="number"
                   v-model.number="args[row.name]" />
            <input v-else :id="`mcp-${row.name}`" type="text"
                   v-model="args[row.name]" />
            <span v-if="row.spec.description" class="faint small">
              {{ row.spec.description }}</span>
          </div>
          <button class="primary" :disabled="trialBusy" @click="runTrial">
            {{ trialBusy ? '调用中…' : '调用' }}</button>
        </div>

        <p v-if="trialLocal" class="warn small">{{ trialLocal }}</p>
        <pre v-if="trialJson" class="config mono"
             :class="trialResult && trialResult.ok ? '' : 'badbox'">{{
               trialJson }}</pre>
      </section>
    </div>
  </div>
</template>

<style scoped>
.mcp { display: flex; flex-direction: column; height: 100%; padding: 12px;
       gap: 8px; overflow-y: auto; }
.head { display: flex; align-items: center; gap: 8px; }
.spacer { flex: 1; }
.cols { display: grid; grid-template-columns: 1fr; gap: 12px; }
.card { padding: 12px; }
.copyrow { display: flex; align-items: center; gap: 8px; margin: 6px 0; }
.config { margin: 6px 0; padding: 10px; background: var(--panel-2, #10151c);
          border: 1px solid var(--line, #263241); border-radius: 6px;
          font-size: 11px; white-space: pre-wrap; word-break: break-all;
          max-height: 260px; overflow-y: auto; }
.badbox { border-color: var(--bad, #d66); }
.kv { display: grid; grid-template-columns: 64px 1fr; gap: 3px 8px; margin: 8px 0 0;
      font-size: 12px; }
.kv dt { color: var(--faint); }
.kv dd { margin: 0; word-break: break-all; }
.tools { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 12px; }
.tools th { text-align: left; color: var(--faint); font-weight: 500;
            padding: 3px 8px 3px 0; border-bottom: 1px solid var(--line, #263241); }
.tools td { padding: 4px 8px 4px 0; vertical-align: top;
            border-bottom: 1px solid var(--line, #263241); }
.tools tr { cursor: pointer; }
.tools tr.on td { background: var(--hover, rgba(120, 160, 255, .08)); }
.tools .desc { color: var(--faint); }
.trialrow { display: flex; align-items: center; gap: 8px; margin-top: 8px;
            flex-wrap: wrap; }
.toolsel { min-width: 160px; }
.form { display: flex; flex-direction: column; gap: 6px; margin-top: 8px;
        max-width: 720px; }
.field { display: grid; grid-template-columns: 150px 220px 1fr; gap: 8px;
         align-items: center; font-size: 12px; }
.field label b { margin-left: 2px; }
.small { font-size: 11px; }
code { font-size: 11px; }
</style>
