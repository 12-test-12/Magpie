/**
 * 风险页**看得见分数、为什么、攻击面**（5.5 的交付标准）。
 *
 * 为什么这条要有：`npm run build` 只能证明**编译**通过，模板里写错一个字段名
 * （`f.contribution` 写成 `f.contrib`）照样编译通过，只有挂起来跑一遍才会炸 ——
 * 而这个仓库里没有浏览器可以点开看。
 *
 * 假数据就是 `GET /api/v1/risk` 的真实形状（后端 `core/risk.overview` 产出）：
 * 这一页**不许自己判**，数字全是后端给的 —— 前端再算一次就是第二份真相，
 * 与 CLI / API 分叉时不会有任何东西变红。
 */

import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import RiskView from '../src/views/RiskView.vue'
import { store } from '../src/store.js'

function factor(id, count, weight, evidence) {
  return {
    id, label: id, weight, count, contribution: weight * count,
    evidence: evidence ? [evidence] : [], evidence_total: count,
  }
}

function riskResponse(overrides = {}) {
  const data = Object.assign({
    query: 'risk',
    rule_version: '1',
    rules_digest: 'sha256:c0ffee',
    project_id: 1,
    score: {
      total: 245,
      factors: [
        factor('vuln_open.critical', 2, 100,
          { type: 'observation', id: 1, label: 'CVE-2021-44228 · critical' }),
        factor('vuln_open.high', 1, 40,
          { type: 'observation', id: 2, label: 'exposed-git-config · high' }),
        factor('inbox_open.low', 1, 2, { type: 'inbox_item', id: 7,
                                         label: 'evidence_missing · low' }),
        factor('untested', 0, 3, null),
        factor('secret', 0, 25, null),
      ],
    },
    attack_surface: {
      hosts: [
        { host: 'app.example.com',
          services: [{ entity_id: 3, key: 'app.example.com:443',
                       products: ['nginx@1.24.0'],
                       open_vulns: {}, open_vuln_total: 2 }],
          endpoints: [{ entity_id: 9, key: 'app.example.com/login',
                        service_entity_id: 3 }],
          open_vulns: {}, open_vuln_total: 2,
          service_count: 1, endpoint_count: 1 },
        { host: 'bare.example.com', services: [], endpoints: [],
          open_vulns: {}, open_vuln_total: 0,
          service_count: 0, endpoint_count: 0 },
      ],
      totals: { hosts: 2, services: 1, endpoints: 1,
                open_vulns: {}, open_vuln_total: 2, unmapped_open_vulns: [] },
    },
    facts: { vuln_total: 3, vuln_by_severity: {}, vuln_by_state: { open: 3 },
             inbox_open: 1, untested: 0, out_of_scope: 0,
             services: 1, endpoints: 1, secrets: 0 },
  }, overrides)
  return {
    ok: true, status: 200,
    json: async () => ({ ok: true, meta: {}, data }),
  }
}

function mountRisk(payload, { requested = [] } = {}) {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
    requested.push(String(url))
    if (String(url).includes('/risk')) return payload
    return { ok: true, status: 200, json: async () => ({ ok: true, data: [] }) }
  })
  store.project = 'demo'
  return mount(RiskView)
}

describe('风险页', () => {
  it('总分、逐因子行（含依据）、攻击面主机都渲染出来', async () => {
    const wrapper = mountRisk(riskResponse())
    await vi.waitFor(() => expect(wrapper.text()).toContain('逐因子分解'))

    const text = wrapper.text()
    expect(text).toContain('245')                       // 总分
    // 非零因子逐行可见，且**带着依据**（黑盒总分是本步的失败模式）
    expect(text).toContain('vuln_open.critical')
    expect(text).toContain('CVE-2021-44228 · critical')
    expect(text).toContain('2 × 100')
    expect(text).toContain('inbox_open.low')
    // 0 命中的因子不铺行（摘要里另有归属）
    expect(text).not.toContain('secret')
    // 攻击面：主机、服务（带产品）、接口计数
    expect(text).toContain('app.example.com')
    expect(text).toContain('app.example.com:443（nginx@1.24.0）')
    expect(text).toContain('bare.example.com')
    expect(text).toContain('规则 v1')
  })

  it('打到后端的请求是 /risk?project=demo（参数原样带项目）', async () => {
    const requested = []
    mountRisk(riskResponse(), { requested })
    await vi.waitFor(() => expect(
      requested.some((u) => u.split('?')[0].endsWith('/risk'))).toBe(true))
    expect(requested.find((u) => u.includes('/risk')))
      .toBe('/api/v1/risk?project=demo')
  })

  it('全部 0 命中时说是"结果不是错误"，而不是留一块空白', async () => {
    const wrapper = mountRisk({
      ok: true, status: 200,
      json: async () => ({
        ok: true, meta: {},
        data: {
          query: 'risk', rule_version: '1', rules_digest: 'sha256:c0ffee',
          project_id: 1,
          score: { total: 0, factors: [factor('untested', 0, 3, null)] },
          attack_surface: { hosts: [], totals: { hosts: 0, services: 0,
                            endpoints: 0, open_vulns: {}, open_vuln_total: 0,
                            unmapped_open_vulns: [] } },
          facts: { vuln_total: 0, vuln_by_severity: {}, vuln_by_state: {},
                   inbox_open: 0, untested: 0, out_of_scope: 0,
                   services: 0, endpoints: 0, secrets: 0 },
        },
      }),
    })
    await vi.waitFor(() => expect(wrapper.text()).toContain('逐因子分解'))
    expect(wrapper.text()).toContain('全部因子 0 命中')
    expect(wrapper.text()).toContain('零命中 —— 这是结果，不是错误')
    expect(wrapper.text()).toContain('没有暴露的 service')
  })

  it('没有 found_on 落点的漏洞要显眼报出来（攻击面不许假装完整）', async () => {
    const payload = riskResponse()
    const original = payload.json
    payload.json = async () => {
      const body = await original()
      body.data.attack_surface.totals.unmapped_open_vulns = [
        { entity_id: 42, key: 'orphan-cve@https://x.example.com/', severity: 'high' },
      ]
      return body
    }
    const wrapper = mountRisk(payload)
    await vi.waitFor(() => expect(wrapper.text()).toContain('攻击面'))
    const text = wrapper.text()
    expect(text).toContain('1 条未处置漏洞没有 found_on 落点')
    expect(text).toContain('orphan-cve@https://x.example.com/')
  })

  it('请求失败时把错误摆出来，而不是留一片空白', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => {
      throw new Error('E_PROJECT_NOT_FOUND: 项目不存在')
    })
    store.project = 'gone'
    const wrapper = mount(RiskView)
    await vi.waitFor(() => expect(wrapper.text())
      .toContain('E_PROJECT_NOT_FOUND'))
  })
})
