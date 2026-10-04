/**
 * 域名视图（0.19.0 · 视图层隔离的前端那半）三件事：
 *
 *   1. **裁剪进 URL**：`setDomainFocus` 必须让 `/graph?…&under_domain=`，
 *      并清掉邻域高亮 —— 归属推导在服务端（`limit=500` 本地筛看不见第 501
 *      个），本地状态改了不重新载图 = 画布纹丝不动。
 *   2. **分段行的顺序与形状**：镜头页「全部」模式按域名分段，未归属垫底，
 *      分段行跨列且不吃点击（它没有 id）。
 *   3. **横幅是出口**：裁剪是服务端做的，画布上必须有常驻的「退出」。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

function jsonResponse(data) {
  return { ok: true, status: 200, json: async () => ({ ok: true, data, meta: {} }) }
}

const EMPTY_GRAPH = { nodes: [], edges: [], layout: {}, project_id: 1 }

beforeEach(() => {
  vi.resetModules()
})

// ---------------------------------------------------------------------------
// 1. setDomainFocus → URL 带参
// ---------------------------------------------------------------------------

describe('域名视图的裁剪走 URL', () => {
  function stubGraphOnly(calls) {
    global.fetch = vi.fn(async (url) => {
      calls.push(String(url))
      if (String(url).includes('/graph')) return jsonResponse(EMPTY_GRAPH)
      throw new Error(`这个测试不该请求 ${url}`)
    })
  }

  it('setDomainFocus 带 under_domain、清 focusId；clearDomainFocus 摘掉', async () => {
    const calls = []
    stubGraphOnly(calls)
    const { store } = await import('../src/store.js')

    store.setFilter({ focusId: 7 })
    await store.setDomainFocus('example.com')
    expect(store.filter.underDomain).toBe('example.com')
    expect(store.filter.focusId).toBeNull()          // 两层高亮会被读成两层筛选
    expect(calls.at(-1)).toContain('under_domain=example.com')

    await store.clearDomainFocus()
    expect(store.filter.underDomain).toBeNull()
    expect(calls.at(-1)).not.toContain('under_domain')
  })

  it('clearFilter 与换项目都把域名视图一起清掉（且真的重新载图）', async () => {
    const calls = []
    global.fetch = vi.fn(async (url) => {
      const href = String(url)
      calls.push(href)
      if (href.includes('/graph')) return jsonResponse(EMPTY_GRAPH)
      if (href.includes('/projects')) return jsonResponse([])
      if (href.includes('/meta')) return jsonResponse({ counts: {}, kinds: [] })
      if (href.includes('/tags')) return jsonResponse([])
      throw new Error(`这个测试不该请求 ${href}`)
    })
    const { store } = await import('../src/store.js')

    await store.setDomainFocus('example.com')
    await store.clearFilter()
    expect(store.filter.underDomain).toBeNull()
    expect(calls.at(-1)).not.toContain('under_domain')   // 清了标志就必须重载

    // 换项目：A 项目的域名在 B 项目多半查无此人 —— 带着它 = 永远空着的画布
    await store.selectProject('demo')
    expect(store.filter.underDomain).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// 2. 镜头页：分段行顺序 + 下拉懒加载
// ---------------------------------------------------------------------------

const DOMAIN_ROWS = [
  { id: 1, kind: 'domain', entity_key: 'app.example.com', lifecycle: 'active',
    domain: 'app.example.com' },
  { id: 2, kind: 'domain', entity_key: 'other.com', lifecycle: 'active',
    domain: 'other.com' },
]
const HOST_ROWS = [
  { id: 3, kind: 'host', entity_key: 'www.app.example.com', lifecycle: 'active',
    domain: 'app.example.com' },
]
const IP_ROWS = [
  { id: 9, kind: 'ip', entity_key: '203.0.113.10', lifecycle: 'active',
    domain: null },
]

async function mountLens() {
  const { store } = await import('../src/store.js')
  store.lenses.assets = {
    lens: 'assets',
    kinds: [{ kind: 'domain', count: 2 }, { kind: 'host', count: 1 },
            { kind: 'ip', count: 1 }],
  }
  const LensView = (await import('../src/views/LensView.vue')).default
  const wrapper = mount(LensView, { props: { lens: 'assets', label: '资产' } })
  await flushPromises()
  return { wrapper, store }
}

describe('镜头页按域名分段', () => {
  it('全部模式：域名按字典序分段、未归属垫底、分段行跨列', async () => {
    global.fetch = vi.fn(async (url) => {
      const href = String(url)
      if (href.includes('kind=domain')) return jsonResponse(DOMAIN_ROWS)
      if (href.includes('kind=host')) return jsonResponse(HOST_ROWS)
      if (href.includes('kind=ip')) return jsonResponse(IP_ROWS)
      throw new Error(`这个测试不该请求 ${href}`)
    })
    const { wrapper } = await mountLens()

    // 请求带上了 with_domain=1（分组字段是服务端补的，不是前端猜的）
    expect(global.fetch.mock.calls.some(
      ([url]) => String(url).includes('with_domain=1'))).toBe(true)

    const rows = wrapper.findAll('tbody tr')
    expect(rows.map((tr) => (tr.classes().includes('group-row') ? 'G' : 'R')))
      .toEqual(['G', 'R', 'R', 'G', 'R', 'G', 'R'])
    const labels = wrapper.findAll('tr.group-row td').map((td) => td.text())
    expect(labels).toEqual(['app.example.com', 'other.com', '未归属'])
    // 跨列一行（assets 镜头 6 列），不是只占第一格
    expect(wrapper.find('tr.group-row td').attributes('colspan')).toBe('6')
  })

  it('选中域名：带 under_domain= 服务端裁剪，分段行不再出现', async () => {
    const calls = []
    // 桩替服务端做**最小**裁剪（按 domain 字段），模拟真实后端 —— 真正的
    // 归属推导在 `core/query.under_domain_ids`，由后端测试钉住，这里只验
    // "参数送出去了、页面按送出去的结果渲染"。
    const scoped = (rows, href) => {
      const match = href.match(/under_domain=([^&]+)/)
      if (!match) return rows
      const domain = decodeURIComponent(match[1])
      return rows.filter((row) => row.domain === domain)
    }
    global.fetch = vi.fn(async (url) => {
      const href = String(url)
      calls.push(href)
      if (href.includes('kind=domain')) return jsonResponse(scoped(DOMAIN_ROWS, href))
      if (href.includes('kind=host')) return jsonResponse(scoped(HOST_ROWS, href))
      if (href.includes('kind=ip')) return jsonResponse(scoped(IP_ROWS, href))
      throw new Error(`这个测试不该请求 ${href}`)
    })
    const { wrapper } = await mountLens()

    const select = wrapper.find('.domain-pick select')
    expect(select.exists()).toBe(true)
    // 选项是懒加载的：先聚焦把清单拉回来，才选得中（真实用户也这两步）
    await select.trigger('focus')
    await flushPromises()
    await select.setValue('app.example.com')
    await flushPromises()

    expect(calls.some((href) => href.includes('under_domain=app.example.com')))
      .toBe(true)
    expect(wrapper.findAll('tr.group-row')).toHaveLength(0)  // 一个分段无须标
    const keys = wrapper.findAll('tbody tr td:first-child')
      .map((td) => td.text())
    expect(keys.some((text) => text.includes('other.com'))).toBe(false)
    expect(keys.some((text) => text.includes('203.0.113.10'))).toBe(false)
    expect(keys.some((text) => text.includes('www.app.example.com'))).toBe(true)
  })

  it('域名下拉懒加载：第一次聚焦才拉清单，按项目缓存', async () => {
    const calls = []
    global.fetch = vi.fn(async (url) => {
      const href = String(url)
      calls.push(href)
      if (href.includes('kind=domain')) return jsonResponse(DOMAIN_ROWS)
      if (href.includes('kind=host')) return jsonResponse(HOST_ROWS)
      if (href.includes('kind=ip')) return jsonResponse(IP_ROWS)
      throw new Error(`这个测试不该请求 ${href}`)
    })
    const { wrapper, store } = await mountLens()

    // 挂载时只查镜头行，不查域名清单（懒）
    const domainListCalls = () => calls.filter(
      (href) => href.includes('kind=domain') && !href.includes('with_domain'))
    expect(domainListCalls()).toHaveLength(0)
    expect(wrapper.findAll('.domain-pick option')).toHaveLength(1)  // 只有「全部」

    await wrapper.find('.domain-pick select').trigger('focus')
    await flushPromises()
    expect(domainListCalls()).toHaveLength(1)
    expect(store.domains.rows.map((row) => row.entity_key))
      .toEqual(['app.example.com', 'other.com'])
    const options = wrapper.findAll('.domain-pick option').map((o) => o.text())
    expect(options).toEqual(['全部', 'app.example.com', 'other.com'])

    // 再聚焦一次不重取（项目没变）
    await wrapper.find('.domain-pick select').trigger('focus')
    await flushPromises()
    expect(domainListCalls()).toHaveLength(1)
  })
})

// ---------------------------------------------------------------------------
// 3. 详情面板的域名入口
// ---------------------------------------------------------------------------

describe('详情面板：域名实体的主按钮', () => {
  it('「只看它的资产」进视图、视图开着换「退出」；非域名没有这个按钮', async () => {
    global.fetch = vi.fn(async () => jsonResponse(EMPTY_GRAPH))
    const { store } = await import('../src/store.js')
    const DetailPanel = (await import('../src/components/DetailPanel.vue')).default
    const base = {
      observations: [], relations: [], tags: [],
      manual: { protected_fields: [] }, open_inbox: [], timeline: [],
    }
    const buttons = (wrapper, label) => wrapper.findAll('button')
      .filter((button) => button.text() === label)

    store.selectedMerges = []
    store.selectedDetail = {
      ...base,
      entity: { id: 42, entity_key: 'example.com', kind: 'domain',
                lifecycle: 'active' },
    }
    const wrapper = mount(DetailPanel)
    await flushPromises()

    expect(buttons(wrapper, '只看它的资产')).toHaveLength(1)
    await buttons(wrapper, '只看它的资产')[0].trigger('click')
    await flushPromises()
    expect(store.filter.underDomain).toBe('example.com')
    // 互斥单按钮：视图开着，入口按钮让位给出口（一个状态一个动作）
    expect(buttons(wrapper, '只看它的资产')).toHaveLength(0)
    expect(buttons(wrapper, '退出域名视图')).toHaveLength(1)

    await buttons(wrapper, '退出域名视图')[0].trigger('click')
    await flushPromises()
    expect(store.filter.underDomain).toBeNull()
    expect(buttons(wrapper, '只看它的资产')).toHaveLength(1)

    // 非域名实体没有这个入口 —— 对 ip 问"只看它的资产"是问法错（E_NOT_A_DOMAIN）
    store.selectedDetail = {
      ...base,
      entity: { id: 9, entity_key: '203.0.113.10', kind: 'ip',
                lifecycle: 'active' },
    }
    await wrapper.vm.$nextTick()
    expect(buttons(wrapper, '只看它的资产')).toHaveLength(0)
    wrapper.unmount()
  })
})

// ---------------------------------------------------------------------------
// 4. 画布横幅：常驻出口
// ---------------------------------------------------------------------------

describe('画布上的域名横幅', () => {
  it('视图开着时显示域名与退出按钮，退出后消失', async () => {
    global.fetch = vi.fn(async () => jsonResponse(EMPTY_GRAPH))
    const { store } = await import('../src/store.js')
    await store.setDomainFocus('example.com')

    const GraphCanvas = (await import('../src/components/GraphCanvas.vue')).default
    const wrapper = mount(GraphCanvas, {
      global: {
        stubs: {
          VueFlow: { name: 'VueFlow', props: ['nodes', 'edges'],
                     template: '<div class="flow" />' },
          Background: true,
          Controls: true,
        },
      },
    })
    await flushPromises()

    const banner = wrapper.find('.domain-banner')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('example.com')
    expect(banner.text()).toContain('个实体')

    await banner.find('button').trigger('click')
    await flushPromises()
    expect(store.filter.underDomain).toBeNull()
    expect(wrapper.find('.domain-banner').exists()).toBe(false)
    wrapper.unmount()
  })
})
