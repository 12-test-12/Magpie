/**
 * 画布交互的两个修复（0.20.0）：
 *
 *   1. **滑动**：原来 `pan-on-drag=[1,2]` 把左键排除在平移之外（普通拖拽什么
 *      也不发生），`pan-on-scroll` 又让滚轮平移而不是缩放 —— 两个图工具的
 *      标准手势都不成立，这就是"画布里无法滑动"。现在左键拖 = 平移（true），
 *      滚轮缩放走 Vue Flow 的默认（不传即默认）。
 *   2. **坏链**：详情面板点关系，目标不在 `limit=500` 的载入窗口里 —— 旧版
 *      一句"画布上找不到"了事。`ensureVisible` 按 id 拉邻居合入，详情照开。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

function jsonResponse(data) {
  return { ok: true, status: 200, json: async () => ({ ok: true, data, meta: {} }) }
}

beforeEach(() => {
  vi.resetModules()
})

describe('滑动 / 缩放的手势', () => {
  it('左键拖 = 平移（panOnDrag=true），不再把左键排除在外', async () => {
    global.fetch = vi.fn(async () => jsonResponse(
      { nodes: [], edges: [], layout: {}, notes: [], total: 0, project_id: 1 }))
    const GraphCanvas = (await import('../src/components/GraphCanvas.vue')).default
    const wrapper = mount(GraphCanvas, {
      global: {
        stubs: {
          VueFlow: {
            name: 'VueFlow',
            props: ['nodes', 'edges', 'panOnDrag', 'panOnScroll'],
            template: '<div class="flow" />',
          },
          Background: true,
          Controls: true,
        },
      },
    })
    await flushPromises()
    const flow = wrapper.findComponent({ name: 'VueFlow' })
    expect(flow.props('panOnDrag')).toBe(true)
    // 滚轮缩放是 Vue Flow 默认（zoomOnScroll=true）；显式传 panOnScroll 就是
    // 把滚轮让给平移 —— 断言它**没被传**，防止哪天又加回去。
    expect(flow.props('panOnScroll')).toBeUndefined()
    wrapper.unmount()
  })
})

describe('关系跳转不再断链', () => {
  it('目标不在窗口 → 拉邻居按 id 合入，然后打开详情', async () => {
    const calls = []
    const detail = {
      entity: { id: 2, entity_key: '9.9.9.9', kind: 'ip', lifecycle: 'active' },
      observations: [], relations: [], tags: [],
      manual: { protected_fields: [] }, open_inbox: [], timeline: [],
    }
    global.fetch = vi.fn(async (url) => {
      const href = String(url)
      calls.push(href)
      if (href.includes('/graph/2/neighbors')) {
        return jsonResponse({
          nodes: [
            { id: 2, kind: 'ip', lifecycle: 'active', entity_key: '9.9.9.9' },
            { id: 1, kind: 'domain', lifecycle: 'active', entity_key: 'a.example.com' },
          ],
          edges: [{ id: 7, src: 1, dst: 2, rel: 'resolves_to', confidence: 'high',
                    src_key: 'a.example.com', dst_key: '9.9.9.9' }],
          layout: {}, notes: [], total: 2, project_id: 1, root_id: 2, depth: 1,
        })
      }
      if (href.includes('/graph')) {
        return jsonResponse({
          nodes: [{ id: 1, kind: 'domain', lifecycle: 'active',
                    entity_key: 'a.example.com' }],
          edges: [], layout: {}, notes: [], total: 1, project_id: 1,
        })
      }
      if (href.includes('/entities/2/merges')) return jsonResponse([])
      if (href.includes('/entities/2')) return jsonResponse(detail)
      throw new Error(`这个测试不该请求 ${href}`)
    })
    const { store } = await import('../src/store.js')
    await store.loadGraph()
    expect(store.counts.entities).toBe(1)
    // 类型筛选只留 domain —— 目标（ip）合入后仍被本地谓词挡住
    store.setFilter({ kinds: ['domain'] })

    await store.ensureVisible(2)
    expect(calls.some((href) => href.includes('/graph/2/neighbors'))).toBe(true)
    // 按 id 去重合入：1 号不重复，2 号补进来
    expect(store.counts.entities).toBe(2)
    // 合入后仍被挡住时详情也开着 —— 日志里说出来，不静默
    expect(store.selectedDetail?.entity?.id).toBe(2)
    expect(store.logLines.some((line) => line.text.includes('不在当前画布窗口')))
      .toBe(true)
  })
})
