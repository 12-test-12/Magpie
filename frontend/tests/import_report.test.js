/**
 * 多文件导入的报告可见性（0.18.0）—— 从假 SSE 到界面的整条链。
 *
 * 用户看到的 bug 是"多个文件导入后内容无法查看"，它其实是三处病的叠加，
 * 这条测试对着三处各钉一根桩：
 *
 *   1. **进度不回零**：`progress` 的 records 是逐文件的 —— 第二个文件开始后
 *      必须显示 `base + 本文件进度`（两个文件之和），而不是 5。
 *   2. **逐文件状态可见**：`file_start` / `file_done` 是显式的「[i/n]」行，
 *      `done` 之后拉 `GET /imports/{id}` 渲染完成态表（path/state/counts/job_id）。
 *   3. **坏行明细真渲染**：LogDrawer 的坏行页签列出流式明细 + 计数差 + 全量落盘
 *      路径（来自报告的 `badlines_report`）。
 *
 * 假后端必须按**真实形状**喂：事件帧 `{events: [{type, data, terminal}]}`、
 * 报告 `{files: [{path, state, counts, job_id, badlines_report}]}`。
 */

import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ImportDialog from '../src/views/ImportDialog.vue'
import LogDrawer from '../src/components/LogDrawer.vue'
import { store } from '../src/store.js'

/** 可手动推事件的假 EventSource —— api.js 在调用点查全局，stubGlobal 即可。 */
class FakeEventSource {
  static instances = []
  constructor(url) {
    this.url = String(url)
    this.listeners = {}
    this.closed = false
    FakeEventSource.instances.push(this)
  }
  addEventListener(type, cb) {
    (this.listeners[type] = this.listeners[type] || []).push(cb)
  }
  close() { this.closed = true }
  emit(payload) {
    for (const cb of this.listeners.batch || []) {
      cb({ data: JSON.stringify(payload) })
    }
  }
}

const REPORT = {
  ok: true, meta: {},
  data: {
    import_id: 'imp-1', state: 'done', paths: ['D:/in/a.txt', 'D:/in/b.txt'],
    job_ids: [11, 12], totals: {}, problems: [], error: null,
    report: {
      kind: 'import', dry_run: false, job_ids: [11, 12], problems: [],
      files: [
        { path: 'D:/in/a.txt', state: 'done', job_id: 11,
          counts: { records: 10, bad_lines: 2 } },
        { path: 'D:/in/b.txt', state: 'partial', job_id: 12,
          counts: { records: 5, bad_lines: 1 },
          badlines_report: 'data/reports/12.badlines.txt' },
      ],
    },
  },
}

function json(data) {
  return { ok: true, status: 200, json: async () => data }
}

function stubFetch() {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    const href = String(url)
    const method = ((options && options.method) || 'GET').toUpperCase()
    if (method === 'POST' && href.endsWith('/api/v1/imports')) {
      return json({ ok: true, data: { import_id: 'imp-1', state: 'queued' },
                    meta: {} })
    }
    if (method === 'GET' && href.includes('/api/v1/imports/imp-1')) {
      return json(REPORT)
    }
    if (href.includes('/projects')) return json({ ok: true, data: [], meta: {} })
    if (href.includes('/graph')) {
      return json({ ok: true, data: { nodes: [], edges: [], layout: {} },
                    meta: {} })
    }
    if (href.includes('/meta')) {
      return json({ ok: true, data: { counts: {}, kinds: [] }, meta: {} })
    }
    return json({ ok: true, data: {}, meta: {} })
  })
}

/** 提交导入并拿到那条假 SSE 连接。 */
async function submitTwoFiles(wrapper) {
  wrapper.find('textarea').element.value = 'D:/in/a.txt\nD:/in/b.txt'
  await wrapper.find('textarea').trigger('input')
  const start = wrapper.findAll('button')
    .find((b) => b.text().includes('开始导入'))
  await start.trigger('click')
  await vi.waitFor(() => expect(FakeEventSource.instances.length).toBe(1))
  return FakeEventSource.instances[FakeEventSource.instances.length - 1]
}

beforeEach(() => {
  FakeEventSource.instances = []
  store.resetImportProgress()
  store.clearLog()
  vi.stubGlobal('EventSource', FakeEventSource)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('多文件导入：进度累计与逐文件状态', () => {
  it('第二个文件的进度不回零 —— 显示的是两文件之和', async () => {
    stubFetch()
    const wrapper = mount(ImportDialog, { attachTo: document.body })
    // LogDrawer 由外壳渲染，不在 ImportDialog 里 —— 单独挂一个读同一份 store
    const log = mount(LogDrawer, { attachTo: document.body })
    const source = await submitTwoFiles(wrapper)

    source.emit({ events: [
      { type: 'file_start',
        data: { file: 'a.txt', path: 'D:/in/a.txt', index: 0, total: 2 } },
      { type: 'progress',
        data: { file: 'a.txt', records: 10, line: 100,
                counts: { records: 10, bad_lines: 2 },
                bad_lines: [{ line: 5, reason: '坏行1', raw: 'x1' }] } },
      { type: 'file_done',
        data: { file: 'a.txt', path: 'D:/in/a.txt', index: 0, total: 2,
                state: 'done', job_id: 11,
                counts: { records: 10, bad_lines: 2 } } },
      { type: 'file_start',
        data: { file: 'b.txt', path: 'D:/in/b.txt', index: 1, total: 2 } },
      { type: 'progress',
        data: { file: 'b.txt', records: 5, line: 50,
                counts: { records: 5, bad_lines: 1 },
                bad_lines: [{ line: 9, reason: '坏行2', raw: 'x2' }] } },
    ] })
    await wrapper.vm.$nextTick()

    const p = store.importProgress
    // 回归核心：本文件 5 条 + 已完成文件的 base 10 条 = 15，不是 5
    expect(p.records).toBe(15)
    expect(p.badLines).toBe(3)               // 2 + 1 同样是累计
    expect(p.baseRecords).toBe(10)
    expect(p.fileIndex).toBe(2)
    expect(p.fileTotal).toBe(2)
    expect(p.line).toBe(50)                  // 行号是**当前文件**的（数据层口径）
    expect(p.badLinesDetail).toHaveLength(2)
    expect(p.badLinesDetail[1]).toMatchObject({ file: 'b.txt', line: 9 })
    // 日志说人话：[i/n] + 累计口径，而不是倒一段 JSON
    expect(log.text()).toContain('开始文件 [2/2] b.txt')
    expect(log.text()).toContain('累计 15 条记录')
  })

  it('done 之后拉报告渲染完成态表；坏行页签真渲染', async () => {
    stubFetch()
    const wrapper = mount(ImportDialog, { attachTo: document.body })
    const drawer = mount(LogDrawer, { attachTo: document.body })
    const source = await submitTwoFiles(wrapper)

    source.emit({ events: [
      { type: 'file_start',
        data: { file: 'a.txt', path: 'D:/in/a.txt', index: 0, total: 2 } },
      { type: 'progress',
        data: { file: 'a.txt', records: 10, line: 100,
                counts: { records: 10, bad_lines: 2 },
                bad_lines: [{ line: 5, reason: '坏行1', raw: 'x1' }] } },
      { type: 'file_done',
        data: { file: 'a.txt', path: 'D:/in/a.txt', index: 0, total: 2,
                state: 'done', job_id: 11,
                counts: { records: 10, bad_lines: 2 } } },
      { type: 'file_start',
        data: { file: 'b.txt', path: 'D:/in/b.txt', index: 1, total: 2 } },
      { type: 'progress',
        data: { file: 'b.txt', records: 5, line: 50,
                counts: { records: 5, bad_lines: 1 },
                bad_lines: [{ line: 9, reason: '坏行2', raw: 'x2' }] } },
      { type: 'file_done',
        data: { file: 'b.txt', path: 'D:/in/b.txt', index: 1, total: 2,
                state: 'partial', job_id: 12,
                counts: { records: 5, bad_lines: 1 } } },
      { type: 'done', terminal: true,
        data: { import_id: 'imp-1', state: 'done', job_ids: [11, 12] } },
    ] })
    await wrapper.vm.$nextTick()

    // 完成态表：逐文件 path / state / 记录 / job —— "哪个文件没成"看得见
    await vi.waitFor(() => expect(wrapper.text()).toContain('导入报告'))
    expect(wrapper.text()).toContain('D:/in/a.txt')
    expect(wrapper.text()).toContain('D:/in/b.txt')
    expect(wrapper.text()).toContain('partial')
    expect(store.importProgress.files).toHaveLength(2)
    expect(store.importProgress.files[1].job_id).toBe(12)
    expect(store.importProgress.records).toBe(15)   // file_done 收尾后仍是累计

    // LogDrawer：进度行带文件计数；坏行页签列明细 + 计数差 + 落盘路径
    expect(drawer.text()).toContain('文件 2/2')
    expect(drawer.text()).toContain('已完成 2 个文件')
    const badTab = drawer.findAll('button')
      .find((b) => b.text().includes('坏行明细'))
    await badTab.trigger('click')
    await drawer.vm.$nextTick()
    expect(drawer.text()).toContain('坏行1')
    expect(drawer.text()).toContain('b.txt:9')   // file 是 basename，path 才是全路径
    // 明细 2 条 < 计数 3 条 → 必须明说"这是部分"，全量在落盘路径里
    expect(drawer.text()).toContain('明细是流式推送的一部分')
    expect(drawer.text()).toContain('data/reports/12.badlines.txt')
  })
})
