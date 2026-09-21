/**
 * 体检页**看得见五类问题**（3.6 的交付标准：`孤儿/冲突/低置信度/未知字段/越界资产`
 * 五类可见）。
 *
 * 为什么这条要有：`npm run build` 只能证明**编译**通过，模板里写错一个字段名
 * （`health.categorie`）照样编译通过，只有挂起来跑一遍才会炸 —— 而这个仓库里
 * 没有浏览器可以点开看。3.6 之前这一页把五类列在「尚未提供」里，正是因为没有
 * 端点可接；现在接上了，若渲染层漏掉一栏，页面上就又会变成一个"没这回事"的空白。
 *
 * 这一页**不许自己判**这五类：数字全部来自 `/doctor` 的 `data.health`（背后是
 * `core/query.py` 的具名查询，CLI 调同一个），所以这里的假数据就是那个形状。
 */

import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import DoctorView from '../src/views/DoctorView.vue'
import { store } from '../src/store.js'

const CATEGORIES = [
  { key: 'orphan', label: '孤儿', why: '从未有过任何贡献的实体',
    kinds: ['orphan_subject'], count: 2, items: [{ id: 1 }, { id: 2 }], truncated: false },
  { key: 'conflict', label: '冲突', why: '同字段多值',
    kinds: ['value_conflict', 'merge_conflict', 'identity_ambiguous'],
    count: 1, items: [{ id: 3 }], truncated: false },
  { key: 'low_confidence', label: '低置信度', why: '嗅探仲裁拿不准',
    kinds: ['manual_required'], count: 0, items: [], truncated: false },
  { key: 'unknown_field', label: '未知字段', why: '解析器不认识的字段名',
    kinds: ['evidence_missing'], count: 0, items: [], truncated: false },
  { key: 'out_of_scope', label: '越界资产', why: '落在 scope 之外',
    kinds: ['scope_drift'], count: 0, items: [], truncated: false },
]

function healthResponse(categories, { unclaimed = [], notes = [] } = {}) {
  const claimed = categories.reduce((sum, c) => sum + c.count, 0)
  return {
    ok: true, status: 200,
    json: async () => ({
      ok: true, meta: {},
      data: {
        ok: true, violations: [], foreign_key_check: [], sqlite_integrity: 'ok',
        jobs_by_state: [], open_inbox: claimed, raw_files: [],
        project_id: 1, untested_entities: 0,
        health: { query: 'health', project_id: 1, categories,
                  claimed, open_inbox: claimed, unclaimed, notes },
      },
    }),
  }
}

function mountDoctor(payload) {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
    if (String(url).includes('/doctor')) return payload
    return { ok: true, status: 200, json: async () => ({ ok: true, data: [], meta: {} }) }
  })
  store.project = 'demo'
  return mount(DoctorView, { global: { stubs: { VirtualTable: true } } })
}

describe('数据体检页', () => {
  it('五类问题全部渲染出来（名字 + 计数 + 判据来源的 kind）', async () => {
    const wrapper = mountDoctor(healthResponse(CATEGORIES))
    await vi.waitFor(() => expect(wrapper.text()).toContain('五类问题'))
    const text = wrapper.text()
    for (const label of ['孤儿', '冲突', '低置信度', '未知字段', '越界资产']) {
      expect(text).toContain(label)
    }
    // 计数：2 条孤儿与 1 条冲突都要看得见（数字是这一页的全部意义）
    const cells = wrapper.findAll('.five td:first-child').map((c) => c.text())
    expect(cells).toEqual(['2', '1', '0', '0', '0'])
    // 每一行的判定来源（kind）也在，人不用回去查代码才知道数字是从哪来的
    expect(text).toContain('orphan_subject')
    expect(text).toContain('scope_drift')
  })

  it('未决项非零时不说"干净"；全部归零时说出来', async () => {
    const dirty = mountDoctor(healthResponse(CATEGORIES))
    await vi.waitFor(() => expect(dirty.text()).toContain('五类问题'))
    expect(dirty.text()).toContain('未决 3 条')
    expect(dirty.text()).not.toContain('干净 —— 五类全部归零')

    const clean = mountDoctor(healthResponse(
      CATEGORIES.map((c) => ({ ...c, count: 0, items: [] }))))
    await vi.waitFor(() => expect(clean.text()).toContain('五类问题'))
    expect(clean.text()).toContain('干净 —— 五类全部归零')
  })

  it('没有项目时明说"未计算"，而不是拿 0 冒充"没有问题"', async () => {
    const wrapper = mountDoctor({
      ok: true, status: 200,
      json: async () => ({
        ok: true, meta: {},
        data: { ok: true, violations: [], foreign_key_check: [],
                sqlite_integrity: 'ok', jobs_by_state: [], open_inbox: 0,
                raw_files: [] },
      }),
    })
    await vi.waitFor(() => expect(wrapper.text()).toContain('五类问题'))
    expect(wrapper.text()).toContain('未计算')
  })

  it('有没人认领的 kind 时报警（分类表漏了）', async () => {
    const wrapper = mountDoctor(healthResponse(CATEGORIES, {
      unclaimed: [{ kind: 'identity_ambiguous', n: 1 }],
      notes: ['有 1 条未决项不属于五类中的任何一类'],
    }))
    await vi.waitFor(() => expect(wrapper.text()).toContain('五类问题'))
    expect(wrapper.text()).toContain('identity_ambiguous')
  })
})
