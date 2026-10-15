/**
 * 整壳冒烟：**把 App 挂起来**，喂一份假数据，断言关键路径真的渲染出来了。
 *
 * 为什么需要它（而不是只测零件）：`npm run build` 只能证明**编译**通过。
 * 模板里写错一个字段名（`store.counts.entites`）编译一样通过，运行到那一步
 * 才炸 —— 而这个仓库里没有浏览器可以点开看。挂载一次是最接近"打开页面"的
 * 动作：Vue 会把模板真的跑一遍，字段名写错会在这里变成一条红。
 *
 * 覆盖的路径：有项目 + 有数据（主界面）/ 空库（引导卡）/ 没 token。
 */

import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const STUBS = {
  // 让断言落在"我们自己写的壳"上，而不是 Vue Flow 的内部 DOM —— 后者会随
  // 它自己的版本变化，那种红与我们的改动无关。
  VueFlow: { name: 'VueFlow', props: ['nodes', 'edges'], template: '<div class="flow" />' },
  Background: true,
  Controls: true,
}

function jsonResponse(data) {
  return { ok: true, status: 200, json: async () => ({ ok: true, data, meta: {} }) }
}

/** 一个够用的假后端。缺哪个端点，那个端点会**响亮地**报出来。 */
function stubBackend({ projects, graph, vulns = [] }) {
  // `kinds` 必须像真的一样带上 `lens` —— 左栏的镜头清单是**从它推导**出来的
  // （`store.loadLenses` 读 `meta.kinds[].lens`），喂一份空的 kinds 会让左栏
  // 一个镜头都没有，而那测不到我们要测的东西。
  const routes = {
    '/meta': { magpie_version: '0.3.0', schema_version: 5, api_prefix: '/api/v1',
               kinds: [
                 { kind: 'domain', lens: 'assets', identity_rule: '', produced_by: [] },
                 { kind: 'service', lens: 'services', identity_rule: '', produced_by: [] },
                 { kind: 'vuln', lens: 'vuln', identity_rule: '', produced_by: [] },
               ],
               relation_verbs: [], parsers: [], parser_source: 'fixture',
               counts: { projects: projects.length, entities: graph.nodes.length,
                         observations: 0, relations: graph.edges.length, jobs: 0,
                         open_inbox: 2 } },
    '/projects': projects,
    '/graph': graph,
    // 每个 kind 一次 `/entities?kind=…`（查询串已剥掉），所以这三条按 path
    // 没法区分 —— 用 kind 兜底：镜头页只会问它自己那一个 kind。
    '/entities': vulns,
    '/lenses/assets': { lens: 'assets', total: 3,
                        kinds: [{ kind: 'domain', identity_rule: '', produced_by: [],
                                  count: 2 }] },
    '/lenses/services': { lens: 'services', total: 1,
                          kinds: [{ kind: 'service', identity_rule: '', produced_by: [],
                                    count: 1 }] },
    '/lenses/vuln': { lens: 'vuln', total: vulns.length,
                      kinds: [{ kind: 'vuln', identity_rule: '', produced_by: [],
                                count: vulns.length }] },
  }
  const detail = new Map(vulns.map((row) => [row.id, {
    entity: row, observations: row.__observations || [], relations: [], tags: [],
    manual: { protected_fields: [] }, open_inbox: [], timeline: [],
  }]))
  global.fetch = vi.fn(async (url) => {
    const path = String(url).split('?')[0]
    const byId = path.match(/\/entities\/(\d+)$/)
    if (byId) {
      const found = detail.get(Number(byId[1]))
      if (found) return jsonResponse(found)
      throw new Error(`冒烟测试没有这个实体：${url}`)
    }
    for (const [key, value] of Object.entries(routes)) {
      if (path.endsWith(key)) return jsonResponse(value)
    }
    throw new Error(`冒烟测试没准备这个端点：${url}`)
  })
}

/** 让 `setTimeout(0)` 之后的若干轮微任务都跑完（补齐观测是几次 await）。 */
async function flush(times = 6) {
  for (let i = 0; i < times; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0))
  }
}

const SAMPLE_GRAPH = {
  nodes: [
    { id: 1, kind: 'domain', lifecycle: 'active', entity_key: 'app.example.com' },
    { id: 2, kind: 'ip', lifecycle: 'active', entity_key: '203.0.113.10' },
  ],
  edges: [{ id: 1, src: 1, dst: 2, rel: 'resolves_to', confidence: 'high',
            src_key: 'app.example.com', dst_key: '203.0.113.10' }],
  layout: {},
  project_id: 1,
}

async function mountApp() {
  const App = (await import('../src/App.vue')).default
  const wrapper = mount(App, { global: { stubs: STUBS } })
  await new Promise((resolve) => setTimeout(resolve, 0))
  await wrapper.vm.$nextTick()
  return wrapper
}

describe('整壳冒烟', () => {
  beforeEach(() => {
    vi.resetModules()
    window.history.replaceState({}, '', '/?token=smoke-token')
  })

  it('有项目有数据：顶部 / 左栏 / 画布 / 详情 / 抽屉都渲染出来', async () => {
    stubBackend({
      projects: [{ id: 1, name: 'demo', status: 'active', scope_version: 1,
                   scope_hash: 'x', parser_fingerprint: null, scope: {},
                   counts: { entities: 2, observations: 3, relations: 1 } }],
      graph: SAMPLE_GRAPH,
    })
    const wrapper = await mountApp()
    const html = wrapper.html()

    expect(html).toContain('Magpie')
    expect(html).toContain('demo')                       // 项目切换
    expect(html).toContain('导入')                        // 顶部 Run
    expect(html).toContain('只读')                        // 只读徽标常驻
    expect(html).toContain('探索模式')                    // 画布上的模式徽标
    expect(html).toContain('资产')                        // 工作台业务导航
    expect(html).toContain('服务与中间件')
    expect(html).toContain('Inbox')
    expect(html).toContain('导入作业')                    // 底部抽屉页签
    expect(html).not.toContain('URL 里没有 token')
    wrapper.unmount()
  })

  it('空库：显示引导卡而不是空白表格', async () => {
    stubBackend({ projects: [], graph: { nodes: [], edges: [], layout: {} } })
    const wrapper = await mountApp()
    const html = wrapper.html()
    expect(html).toContain('从这里开始')
    expect(html).toContain('建项目')
    wrapper.unmount()
  })

  it('有项目但没数据：引导卡指向导入', async () => {
    stubBackend({
      projects: [{ id: 1, name: 'demo', status: 'active', scope_version: 1,
                   scope_hash: 'x', parser_fingerprint: null, scope: {},
                   counts: { entities: 0, observations: 0, relations: 0 } }],
      graph: { nodes: [], edges: [], layout: {} },
    })
    const wrapper = await mountApp()
    const html = wrapper.html()
    expect(html).toContain('还没有数据')
    expect(html).toContain('开始导入')
    wrapper.unmount()
  })

  it('漏洞镜头：镜头页自己出现，severity / state 可筛', async () => {
    const vulns = [
      { id: 11, kind: 'vuln', lifecycle: 'active', observation_count: 11,
        out_degree: 1, first_seen: 'T1', last_seen: 'T1',
        entity_key: 'CVE-2021-44228@https://app.example.com:443/',
        __observations: [{ semantic_field: 'severity', value: 'critical' },
                         { semantic_field: 'state', value: 'open' }] },
      { id: 12, kind: 'vuln', lifecycle: 'active', observation_count: 10,
        out_degree: 1, first_seen: 'T1', last_seen: 'T1',
        entity_key: 'tech-detect@https://nginx.example.com:443/',
        __observations: [{ semantic_field: 'severity', value: 'info' },
                         { semantic_field: 'state', value: 'open' }] },
    ]
    stubBackend({
      projects: [{ id: 1, name: 'demo', status: 'active', scope_version: 1,
                   scope_hash: 'x', parser_fingerprint: null, scope: {},
                   counts: { entities: 2, observations: 3, relations: 1 } }],
      graph: SAMPLE_GRAPH,
      vulns,
    })
    const { store } = await import('../src/store.js')
    store.setView('vuln')

    const wrapper = await mountApp()
    await flush()
    await wrapper.vm.$nextTick()
    const html = wrapper.html()

    // 镜头是从 `meta.kinds[].lens` 推导出来的，不是代码里写死的一份清单。
    expect(store.lensKeys).toContain('vuln')
    expect(html).toContain('漏洞')
    expect(html).toContain('严重度')
    expect(html).toContain('状态')
    expect(html).toContain('CVE-2021-44228@https://app.example.com:443/')
    expect(html).toContain('tech-detect@https://nginx.example.com:443/')

    // 筛一个 severity：只剩 critical 那一条。选项来自**这一页真的出现过的值**。
    const select = wrapper.find('.facet select')
    expect(select.exists()).toBe(true)
    await select.setValue('critical')
    await wrapper.vm.$nextTick()
    const filtered = wrapper.html()
    expect(filtered).toContain('CVE-2021-44228@https://app.example.com:443/')
    expect(filtered).not.toContain('tech-detect@https://nginx.example.com:443/')
    wrapper.unmount()
  })

  it('URL 里没有 token 时，说的是"去用 magpie app 打的那条 URL"，而不是一个白页', async () => {
    window.history.replaceState({}, '', '/')
    // sessionStorage 也要清：`api.js` 会把 URL 里见过的 token 记在那儿，
    // 好让刷新页面不丢。只清 location 不清它，这条测试会因为上一条留下的
    // token 而**测不到**它要测的东西。
    window.sessionStorage.clear()
    const wrapper = await mountApp()
    expect(wrapper.html()).toContain('URL 里没有 token')
    wrapper.unmount()
  })
})
