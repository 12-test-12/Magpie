/**
 * 项目总览 = 默认首页（评审十一）。
 *
 * 钉三件事：
 *
 *   1. **默认视图是 overview** —— 打开产品第一眼是「这个项目现在怎么样」
 *      （四象限 + 风险概览 + 重点风险资产），不是一张图；改默认视图的人
 *      会在这里看到红，而不是等用户说"怎么一进来就是图"。
 *   2. **CTA 真的进画布** —— 「进入攻击面画布 →」是显式动作：点完 overview
 *      消失、GraphCanvas 出现。只断言 store.view 会漏掉"按钮没接线"。
 *   3. **空库走 EmptyGuide** —— 引导卡长在首页上（app.test 的空状态断言
 *      从此由 OverviewView 承载，这里再钉一遍它自己的分支）。
 *
 * 风险数字走 `GET /risk`（与风险页同一份口径），**刻意没有 7 天趋势**：
 * 后端没有按时间比较资产数的 API，前端自己数会造出第二份口径 —— 注释写在
 * OverviewView 里，这里不为不存在的东西写断言。
 */

import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const STUBS = {
  // 与 app.test 同款：断言落在我们自己的壳上，而不是 Vue Flow 的内部 DOM。
  VueFlow: { name: 'VueFlow', props: ['nodes', 'edges'], template: '<div class="flow" />' },
  Background: true,
  Controls: true,
}

function jsonResponse(data) {
  return { ok: true, status: 200, json: async () => ({ ok: true, data, meta: {} }) }
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

//: `/risk` 夹具：字段形状照 `core/risk` 的返回（score/facts/attack_surface）。
//: 两个主机 —— 一个有未处置漏洞（进榜），一个没有（**不进榜**："重点"的
//: 定义就是有未处置命中的那些）。
const RISK = {
  score: {
    total: 42,
    factors: [
      { id: 'vuln_open', weight: 2, count: 3, contribution: 30 },
      { id: 'secret_hits', weight: 1, count: 7, contribution: 12 },
      { id: 'untested', weight: 1, count: 0, contribution: 0 },
    ],
  },
  facts: {
    vuln_total: 3, vuln_by_state: { open: 2, fixed: 1 },
    inbox_open: 1, untested: 5, out_of_scope: 0, secrets: 7,
  },
  attack_surface: {
    totals: { unmapped_open_vulns: [] },
    hosts: [
      { host: 'victim.example.com',
        services: [{ key: '80/tcp', products: ['nginx'] }],
        endpoint_count: 12, open_vuln_total: 3 },
      { host: 'quiet.example.com', services: [], endpoint_count: 0,
        open_vuln_total: 0 },
    ],
  },
  rules_digest: 'digest-x', rule_version: '2',
}

/** 假后端：在 app.test 的骨架上加 `/risk` 与四象限需要的镜头。 */
function stubBackend({ projects, graph }) {
  const routes = {
    '/meta': { magpie_version: '0.3.0', schema_version: 5, api_prefix: '/api/v1',
               kinds: [
                 { kind: 'domain', lens: 'assets', identity_rule: '', produced_by: [] },
                 { kind: 'ip', lens: 'assets', identity_rule: '', produced_by: [] },
                 { kind: 'service', lens: 'services', identity_rule: '', produced_by: [] },
                 { kind: 'endpoint', lens: 'api', identity_rule: '', produced_by: [] },
                 { kind: 'vuln', lens: 'vuln', identity_rule: '', produced_by: [] },
               ],
               relation_verbs: [], parsers: [], parser_source: 'fixture',
               counts: { projects: projects.length, entities: graph.nodes.length,
                         observations: 0, relations: graph.edges.length, jobs: 0,
                         open_inbox: 0 } },
    '/projects': projects,
    '/graph': graph,
    '/risk': RISK,
    '/workspace/summary': { assets: 3, services: 1, api: 4, evidence: 6,
      pending: 1, findings: 3, failed_jobs: 0, recent_jobs: [],
      count_basis: '当前未合并实体（含退役）；证据含历史记录' },
    '/lenses/assets': { lens: 'assets', total: 3,
                        kinds: [{ kind: 'domain', identity_rule: '', produced_by: [],
                                  count: 2 },
                                { kind: 'ip', identity_rule: '', produced_by: [],
                                  count: 1 }] },
    '/lenses/services': { lens: 'services', total: 1,
                          kinds: [{ kind: 'service', identity_rule: '', produced_by: [],
                                    count: 1 }] },
    '/lenses/api': { lens: 'api', total: 4,
                     kinds: [{ kind: 'endpoint', identity_rule: '', produced_by: [],
                               count: 4 }] },
    '/lenses/vuln': { lens: 'vuln', total: 1,
                      kinds: [{ kind: 'vuln', identity_rule: '', produced_by: [],
                                count: 1 }] },
  }
  global.fetch = vi.fn(async (url) => {
    const path = String(url).split('?')[0]
    for (const [key, value] of Object.entries(routes)) {
      if (path.endsWith(key)) return jsonResponse(value)
    }
    // 作业列表这类"顺手拉一把"的端点：报出来也行（LogDrawer 自己会吞），但
    // 与 app.test 保持一致 —— 缺端点就响亮地失败，别静默返回空。
    throw new Error(`总览测试没准备这个端点：${url}`)
  })
}

async function flush(times = 6) {
  for (let i = 0; i < times; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0))
  }
}

async function mountApp() {
  const App = (await import('../src/App.vue')).default
  const wrapper = mount(App, { global: { stubs: STUBS } })
  await flush()
  await wrapper.vm.$nextTick()
  return wrapper
}

beforeEach(() => {
  vi.resetModules()
  window.history.replaceState({}, '', '/?token=smoke-token')
})

describe('项目总览是默认首页（评审十一）', () => {
  it('打开即是总览：四象限 / 风险概览 / 重点风险资产 / CTA 全在', async () => {
    stubBackend({
      projects: [{ id: 1, name: 'demo', status: 'active', scope_version: 1,
                   scope_hash: 'x', parser_fingerprint: null, scope: {},
                   counts: { entities: 2, observations: 3, relations: 1 } }],
      graph: SAMPLE_GRAPH,
    })
    const { store } = await import('../src/store.js')
    expect(store.view).toBe('overview')                // 初值就是首页

    const wrapper = await mountApp()
    expect(store.view).toBe('overview')                // 启动流程不改视图
    const html = wrapper.html()
    expect(html).toContain('攻击面安全总览')
    expect(html).toContain('进入攻击面画布')

    // 工作台摘要直接来自服务端；卡片可进入对应业务页面。
    const quads = wrapper.findAll('.metric-card')
    expect(quads).toHaveLength(4)
    const quadText = quads.map((q) => q.text())
    expect(quadText[0]).toContain('资产')
    expect(quadText[1]).toContain('服务')
    expect(quadText[2]).toContain('API')
    expect(quadText[3]).toContain('来源证据')

    // 风险概览：分数与事实来自 /risk（界面不自算）
    expect(html).toContain('待关注事项')
    expect(quads[0].find('strong').text()).toBe('3')
    expect(html).toContain('工具发现 · 待核实')

    // 重点风险资产：有未处置漏洞的进榜，0 漏洞的主机不进
    expect(html).toContain('扫描器命中不等同于已确认漏洞')
    expect(html).toContain('未提供时间差分')
    expect(global.fetch.mock.calls.some(([url]) => String(url).includes('/workspace/summary'))).toBe(true)
    expect(global.fetch.mock.calls.some(([url]) => String(url).includes('/risk'))).toBe(false)
    wrapper.unmount()
  })

  it('点「进入攻击面画布 →」：总览消失、画布出现（按钮真接线）', async () => {
    stubBackend({
      projects: [{ id: 1, name: 'demo', status: 'active', scope_version: 1,
                   scope_hash: 'x', parser_fingerprint: null, scope: {},
                   counts: { entities: 2, observations: 3, relations: 1 } }],
      graph: SAMPLE_GRAPH,
    })
    const { store } = await import('../src/store.js')
    const wrapper = await mountApp()
    expect(wrapper.html()).toContain('攻击面安全总览')

    const cta = wrapper.findAll('button')
      .find((b) => b.text().includes('进入攻击面画布'))
    expect(cta).toBeTruthy()
    await cta.trigger('click')
    await wrapper.vm.$nextTick()

    expect(store.view).toBe('explore')
    expect(wrapper.html()).not.toContain('攻击面安全总览')
    expect(wrapper.find('.flow').exists()).toBe(true)   // GraphCanvas 的 VueFlow 桩
    wrapper.unmount()
  })

  it('空库：首页整页渲染 EmptyGuide（引导卡长在首页上）', async () => {
    stubBackend({ projects: [], graph: { nodes: [], edges: [], layout: {} } })
    const { store } = await import('../src/store.js')

    const wrapper = await mountApp()
    expect(store.view).toBe('overview')
    const html = wrapper.html()
    expect(html).toContain('从这里开始')
    expect(html).toContain('建项目')
    expect(html).not.toContain('攻击面安全总览')       // 走的是引导分支，不是总览
    wrapper.unmount()
  })
})
