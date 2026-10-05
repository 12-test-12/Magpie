/**
 * 敏感观测值的默认遮蔽（评审七）—— 右栏「来源」区那一条防线。
 *
 * 为什么要逐分支钉死：这条防线的失败方式全是**静默的** —— 少一个遮蔽，
 * 界面上看起来只是"这个值显示出来了"，没人会把它当成事故。所以每个分支
 * （凭据形态 / 关键词形态 / 非敏感 / 已是掩码形态 / 复制三条路）都单独断言。
 *
 * 边界与后端一致：观测值入库前已经过 `redact_secrets`（前4位+****），
 * **原文从未入库** —— 这里做的是呈现层防线（`ghp_` 这类规则命中的形态
 * 默认不给一眼看全），不是"显示原文"的开关。含 `****` 的值没有可遮的，
 * 因此**不给**显示按钮（给了就是个假开关）。
 */

import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const get = vi.fn()

vi.mock('../src/api.js', () => ({
  API_PREFIX: '/api/v1',
  token: { value: 'test' },
  hasToken: () => true,
  ApiError: class ApiError extends Error {},
  get: (path) => get(path),
  post: vi.fn(async () => ({})),
  patch: vi.fn(async () => ({})),
  put: vi.fn(async () => ({})),
  watchRun: vi.fn(() => () => {}),
  watchImport: vi.fn(() => () => {}),
}))

const store = (await import('../src/store.js')).store
const DetailPanel = (await import('../src/components/DetailPanel.vue')).default

const TOKEN = 'ghp_ABCDEFGHIJKLMNOPQRST9876'

const DETAIL = {
  entity: {
    id: 9, entity_key: 'github_token@ci', kind: 'secret', lifecycle: 'active',
    first_seen: 'T1', last_seen: 'T2',
  },
  observations: [
    { id: 1, origin: 'tool', semantic_field: 'token', value: TOKEN,
      confidence: 'high', source_count: 1 },
    { id: 2, origin: 'tool', semantic_field: 'server', value: 'nginx/1.24.0',
      confidence: 'high', source_count: 1 },
    { id: 3, origin: 'tool', semantic_field: 'conn', value: 'password=hunter2',
      confidence: 'medium', source_count: 1 },
    { id: 4, origin: 'tool', semantic_field: 'stored', value: 'abcd****',
      confidence: 'high', source_count: 1 },
  ],
  relations: [],
  tags: [],
  manual: { protected_fields: [] },
  open_inbox: [],
  timeline: [],
}

/** 每个用例自己声明剪贴板分支：`undefined` = 这个环境没有剪贴板 API。 */
function stubClipboard(writeText) {
  Object.defineProperty(window.navigator, 'clipboard', {
    value: writeText === undefined ? undefined : { writeText },
    configurable: true,
  })
}

async function flush(times = 5) {
  for (let i = 0; i < times; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0))
  }
}

async function mountPanel(detail = DETAIL) {
  store.selectedDetail = detail
  store.selectedMerges = []
  const wrapper = mount(DetailPanel)
  await wrapper.vm.$nextTick()
  return wrapper
}

beforeEach(() => {
  get.mockReset()
  stubClipboard(undefined)
})

afterEach(() => {
  stubClipboard(undefined)
})

describe('敏感观测值默认遮蔽（评审七）', () => {
  it('凭据形态默认给 前4位**** 预览；「显示」展开、「隐藏」收回', async () => {
    const wrapper = await mountPanel()

    const first = wrapper.findAll('.obs li')[0]
    expect(first.find('.value.masked').exists()).toBe(true)
    expect(first.find('.value.masked').text()).toBe('ghp_****')
    expect(first.text()).not.toContain(TOKEN)          // 全串在默认态不出现
    expect(first.find('.obs-acts').text()).toContain('显示')

    await first.findAll('.obs-acts .ghost')[0].trigger('click')
    await wrapper.vm.$nextTick()
    const shown = wrapper.findAll('.obs li')[0]
    expect(shown.find('.value.masked').exists()).toBe(false)
    expect(shown.text()).toContain(TOKEN)
    expect(shown.find('.obs-acts').text()).toContain('隐藏')

    await shown.findAll('.obs-acts .ghost')[0].trigger('click')
    await wrapper.vm.$nextTick()
    const hidden = wrapper.findAll('.obs li')[0]
    expect(hidden.find('.value.masked').exists()).toBe(true)
    expect(hidden.text()).not.toContain(TOKEN)
    wrapper.unmount()
  })

  it('关键词形态（password=…）同样遮；非敏感值一个字都不动', async () => {
    const wrapper = await mountPanel()
    const items = wrapper.findAll('.obs li')

    // password=hunter2 命中关键词正则 → pass****
    expect(items[2].find('.value.masked').text()).toBe('pass****')
    expect(items[2].text()).not.toContain('hunter2')

    // nginx/1.24.0 不是凭据形态 → 原样显示，也没有多余的动作按钮
    expect(items[1].find('.value.masked').exists()).toBe(false)
    expect(items[1].text()).toContain('nginx/1.24.0')
    expect(items[1].find('.obs-acts').exists()).toBe(false)
    wrapper.unmount()
  })

  it('已是掩码形态（****）没有可遮的：显示原文的假开关不出现', async () => {
    const wrapper = await mountPanel()
    const items = wrapper.findAll('.obs li')

    expect(items[3].text()).toContain('abcd****')
    expect(items[3].find('.obs-acts').exists()).toBe(false)
    wrapper.unmount()
  })

  it('遮蔽是按值判定的 —— 非 secret 实体上的凭据形态照样遮', async () => {
    const wrapper = await mountPanel({
      ...DETAIL,
      entity: { ...DETAIL.entity, kind: 'domain', entity_key: 'demo.example.com' },
    })

    const first = wrapper.findAll('.obs li')[0]
    expect(first.find('.value.masked').text()).toBe('ghp_****')
    expect(first.text()).not.toContain(TOKEN)
    wrapper.unmount()
  })

  it('secret 实体把「原文从未入库」说出来，而不是让人去找显示原文的开关', async () => {
    const wrapper = await mountPanel()
    expect(wrapper.text()).toContain('原文从未入库')
    expect(wrapper.html()).toContain('已掩码')          // header 的 🔒 已掩码 chip
    wrapper.unmount()
  })
})

describe('敏感值的「复制」三条路（成功/无 API/被拒都必须可见）', () => {
  it('剪贴板可用：复制的是**展开后的全文**，成功反馈可见', async () => {
    const writeText = vi.fn(async () => {})
    stubClipboard(writeText)
    const wrapper = await mountPanel()

    await wrapper.findAll('.obs li')[0].findAll('.obs-acts .ghost')[1]
      .trigger('click')
    await flush()

    expect(writeText).toHaveBeenCalledTimes(1)
    expect(writeText).toHaveBeenCalledWith(TOKEN)       // 复制的是原文，不是预览
    expect(wrapper.findAll('.obs li')[0].text()).toContain('已复制')
    wrapper.unmount()
  })

  it('没有剪贴板 API：红着说「没有剪贴板 API」，不静默', async () => {
    stubClipboard(undefined)
    const wrapper = await mountPanel()

    await wrapper.findAll('.obs li')[0].findAll('.obs-acts .ghost')[1]
      .trigger('click')
    await flush()

    expect(wrapper.findAll('.obs li')[0].text()).toContain('没有剪贴板 API')
    wrapper.unmount()
  })

  it('writeText 被拒：错误原文可见', async () => {
    stubClipboard(vi.fn(async () => { throw new Error('denied') }))
    const wrapper = await mountPanel()

    await wrapper.findAll('.obs li')[0].findAll('.obs-acts .ghost')[1]
      .trigger('click')
    await flush()

    const text = wrapper.findAll('.obs li')[0].text()
    expect(text).toContain('复制失败')
    expect(text).toContain('denied')
    wrapper.unmount()
  })
})
