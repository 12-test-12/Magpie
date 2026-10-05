/**
 * 「复制给 AI」按钮（5.3）的行为断言 —— 请求打对端点、剪贴板拿到的是
 * Markdown、**每个分支的可见输出**都在界面上。
 *
 * 为什么这条必须逐分支断言：这条功能的失败方式全是"点了没反应"。静默失败
 * 在视觉上与成功一模一样 —— 只断言"调用了 writeText"会漏掉剪贴板回退分支，
 * 而回退分支（非安全上下文 / 权限被拒 / 老浏览器）恰恰是它最容易坏的地方。
 *
 * 剪贴板默认**不给**：jsdom 本来就没有 `navigator.clipboard`，每个用例显式
 * 声明自己要测哪个分支，而不是靠"上一个用例留下了什么"。
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

const MARKDOWN = '# demo.example.com（domain）\n\n## 观测\n- a: b（high，1 个来源）\n'

const DETAIL = {
  entity: {
    id: 42, entity_key: 'demo.example.com', kind: 'domain', lifecycle: 'active',
    first_seen: 'T1', last_seen: 'T2',
  },
  observations: [{ id: 1, origin: 'tool', semantic_field: 'a', value: 'b',
                   confidence: 'high', source_count: 1 }],
  relations: [],
  tags: ['demo'],
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

async function clickCopy(wrapper) {
  const button = wrapper.find('.copy-ai')
  expect(button.exists()).toBe(true)
  await button.trigger('click')
  await flush()
}

beforeEach(() => {
  get.mockReset()
  get.mockResolvedValue({ json: { entity: { id: 42 } }, markdown: MARKDOWN })
  store.selectedDetail = DETAIL
  store.selectedMerges = []
  stubClipboard(undefined)
})

afterEach(() => {
  stubClipboard(undefined)
})

describe('「复制给 AI」按钮（5.3）', () => {
  it('点按钮 → 打对端点 → Markdown 进剪贴板 → 「已复制 N 字」可见', async () => {
    const writeText = vi.fn(async () => {})
    stubClipboard(writeText)
    const wrapper = mount(DetailPanel)
    await wrapper.vm.$nextTick()

    await clickCopy(wrapper)

    // 端点与参数钉死：同源相对路径，depth 用端点的默认（1），界面不另发明口径
    expect(get).toHaveBeenCalledTimes(1)
    expect(get).toHaveBeenCalledWith('/agent/context?entity_id=42&depth=1')
    // 复制的是 **Markdown**，不是 JSON —— "复制给 AI"要的是能直接读懂的那份
    expect(writeText).toHaveBeenCalledTimes(1)
    expect(writeText).toHaveBeenCalledWith(MARKDOWN)
    // 反馈可见（字数 = Markdown 长度，不是 JSON 长度）
    expect(wrapper.text()).toContain(`已复制 ${MARKDOWN.length} 字`)
    expect(wrapper.find('textarea.manual').exists()).toBe(false)
    wrapper.unmount()
  })

  it('剪贴板 API 不在：原文展开 + 手动复制提示，不静默失败', async () => {
    stubClipboard(undefined)
    const wrapper = mount(DetailPanel)
    await wrapper.vm.$nextTick()

    await clickCopy(wrapper)

    const box = wrapper.find('textarea.manual')
    expect(box.exists()).toBe(true)
    expect(box.element.value).toBe(MARKDOWN)
    expect(wrapper.text()).toContain('手动复制')
    // 失败要"红着说"，而不是只把文本摊开 —— 没人会盯着一个正常页面找回退
    expect(wrapper.find('.bad').exists()).toBe(true)
    expect(wrapper.text()).toContain('剪贴板 API')
    expect(wrapper.text()).not.toContain('已复制')
    wrapper.unmount()
  })

  it('writeText 被拒（权限/非安全上下文）：同样展开原文，错误原文可见', async () => {
    stubClipboard(vi.fn(async () => { throw new Error('denied') }))
    const wrapper = mount(DetailPanel)
    await wrapper.vm.$nextTick()

    await clickCopy(wrapper)

    const box = wrapper.find('textarea.manual')
    expect(box.exists()).toBe(true)
    expect(box.element.value).toBe(MARKDOWN)
    expect(wrapper.text()).toContain('剪贴板写入失败')
    expect(wrapper.text()).toContain('denied')
    expect(wrapper.text()).not.toContain('已复制')
    wrapper.unmount()
  })

  it('端点失败：错误写在界面上，剪贴板不被碰（没有半个上下文包）', async () => {
    const writeText = vi.fn(async () => {})
    stubClipboard(writeText)
    get.mockRejectedValueOnce(new Error('E_BOOM: 库炸了'))
    const wrapper = mount(DetailPanel)
    await wrapper.vm.$nextTick()

    await clickCopy(wrapper)

    expect(wrapper.text()).toContain('取上下文失败')
    expect(wrapper.text()).toContain('E_BOOM')
    expect(writeText).not.toHaveBeenCalled()
    expect(wrapper.find('textarea.manual').exists()).toBe(false)
    wrapper.unmount()
  })

  it('连点不重复发请求（按钮取上下文期间是禁用的）', async () => {
    stubClipboard(vi.fn(async () => {}))
    // 让第一发**悬在半空**：mock 立即 resolve 的话，`trigger` 的 nextTick 里
    // 整个流程已经跑完，断言到的是"按钮又可点了"，测不到中间态。
    let release = null
    get.mockReturnValue(new Promise((resolve) => { release = resolve }))
    const wrapper = mount(DetailPanel)
    await wrapper.vm.$nextTick()

    const button = wrapper.find('.copy-ai')
    await button.trigger('click')
    expect(button.attributes('disabled')).toBeDefined()
    expect(button.text()).toContain('正在取上下文')

    await button.trigger('click')          // 还在取的时候再点
    release({ json: { entity: { id: 42 } }, markdown: MARKDOWN })
    await flush()

    expect(get).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('已复制')
    wrapper.unmount()
  })
})

// ---------------------------------------------------------------------------
// secret 实体的二次确认（评审七："非常严重的问题"）
// ---------------------------------------------------------------------------
//
// 上下文包会带上凭据观测 —— secret 实体上"一点就把凭据铺进剪贴板"是不行的。
// 这里钉的是**三段式**：首点只出确认块（0 请求 0 剪贴板）→ 确认才走原来的
// copyForAI（形态与上面 domain 的断言一字不差）→ 取消则一切归零。失败方式
// 是"确认块渲染了但确认按钮没接线"，只测首点抓不到它。
describe('secret 实体的「复制给 AI」二次确认（评审七）', () => {
  const SECRET_DETAIL = {
    ...DETAIL,
    entity: { ...DETAIL.entity, kind: 'secret', entity_key: 'gh_token@ci' },
  }

  it('首点只出确认块：请求 0 次、剪贴板没碰', async () => {
    store.selectedDetail = SECRET_DETAIL
    const writeText = vi.fn(async () => {})
    stubClipboard(writeText)
    const wrapper = mount(DetailPanel)
    await wrapper.vm.$nextTick()

    await clickCopy(wrapper)

    expect(wrapper.find('.confirm').exists()).toBe(true)
    expect(wrapper.find('.copy-ai-confirm').exists()).toBe(true)
    expect(get).not.toHaveBeenCalled()
    expect(writeText).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('点「确认复制」才真的走端点 + 进剪贴板（形态与 domain 完全一致）', async () => {
    store.selectedDetail = SECRET_DETAIL
    const writeText = vi.fn(async () => {})
    stubClipboard(writeText)
    const wrapper = mount(DetailPanel)
    await wrapper.vm.$nextTick()

    await clickCopy(wrapper)                          // 首点 → 确认块
    await wrapper.find('.copy-ai-confirm').trigger('click')
    await flush()

    expect(get).toHaveBeenCalledTimes(1)
    expect(get).toHaveBeenCalledWith('/agent/context?entity_id=42&depth=1')
    expect(writeText).toHaveBeenCalledTimes(1)
    expect(writeText).toHaveBeenCalledWith(MARKDOWN)
    expect(wrapper.text()).toContain(`已复制 ${MARKDOWN.length} 字`)
    // 确认块在 copyFeedback 出现后收起 —— 留着它会让人以为还要再确认一次
    expect(wrapper.find('.confirm').exists()).toBe(false)
    wrapper.unmount()
  })

  it('点「取消」：确认块收起，一个请求都不发', async () => {
    store.selectedDetail = SECRET_DETAIL
    const writeText = vi.fn(async () => {})
    stubClipboard(writeText)
    const wrapper = mount(DetailPanel)
    await wrapper.vm.$nextTick()

    await clickCopy(wrapper)                          // 首点 → 确认块
    await wrapper.find('.confirm .ghost').trigger('click')   // 取消
    await flush()

    expect(wrapper.find('.confirm').exists()).toBe(false)
    expect(get).not.toHaveBeenCalled()
    expect(writeText).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
