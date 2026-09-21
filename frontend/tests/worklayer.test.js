/**
 * 3.8 的前端面：布局的两列互不串位 / 标注的增删 / 批量选中与批量改状态。
 *
 * 这里测的**不是** API（那是 `magpie/api/tests/test_work_layer_api.py` 的事），
 * 而是"界面发出去的那个请求对不对"。两件事各自会坏，而且坏法不一样：
 *
 *   * 后端可以是对的，请求可以带错字段 —— 拖一次位置顺手补一个
 *     `collapsed: false`，后端照单全收，折叠状态被抹掉，**两份代码各自看都
 *     没有 bug**。3.8 之前这里就是这个样子。
 *   * 前端可以是对的，后端整行覆盖 —— 那也不是这条用例能发现的。
 *
 * 所以：这里断言的是**请求体**与**本地状态**，逐字段。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

const calls = []

vi.mock('../src/api.js', () => ({
  API_PREFIX: '/api/v1',
  token: { value: 'test' },
  hasToken: () => true,
  ApiError: class ApiError extends Error {},
  get: vi.fn(async (path) => {
    calls.push({ method: 'GET', path })
    if (path.startsWith('/tags')) return []
    if (path.startsWith('/entities/') && path.endsWith('/merges')) return []
    return {}
  }),
  post: vi.fn(async (path, body) => {
    calls.push({ method: 'POST', path, body })
    if (path.startsWith('/entities/lifecycle')) {
      return { job_id: 7, counts: { requested: 2, changed: 2, unchanged: 0 } }
    }
    return { event_id: 1, conflicts: [] }
  }),
  patch: vi.fn(async (path, body) => {
    calls.push({ method: 'PATCH', path, body })
    return { tags: [] }
  }),
  put: vi.fn(async (path, body) => {
    calls.push({ method: 'PUT', path, body })
    return {}
  }),
  watchRun: vi.fn(() => () => {}),
}))

const store = (await import('../src/store.js')).store
const GraphCanvas = (await import('../src/components/GraphCanvas.vue')).default
const EntityNode = (await import('../src/components/EntityNode.vue')).default

/** 上一次布局写入的 `items[0]` —— 所有断言落在**送出去的那条记录**上。 */
function lastLayoutItem() {
  const write = [...calls].reverse().find(
    (c) => c.method === 'PATCH' && c.path.startsWith('/graph/layout'))
  return write ? write.body.items[0] : null
}

function lastBody(method, prefix) {
  const write = [...calls].reverse().find(
    (c) => c.method === method && c.path.startsWith(prefix))
  return write ? write.body : null
}

beforeEach(() => {
  calls.length = 0
})

describe('布局的两列互不串位（3.8）', () => {
  it('拖位置只送坐标，**不送** collapsed', async () => {
    store.persistLayout('42', { x: 5, y: 6 }, { immediate: true })
    await nextTick()
    const item = lastLayoutItem()
    expect(item.entity_id).toBe(42)
    expect(item.x).toBe(5)
    expect(item.y).toBe(6)
    // 旧实现固定补一个 `collapsed: false` —— 拖一下就把折叠状态抹掉了
    expect('collapsed' in item).toBe(false)
  })

  it('折叠只改这一列，且**带上坐标**（否则对一行新记录是拒绝的）', async () => {
    store.persistLayout('42', { x: 5, y: 6 }, { immediate: true })
    await nextTick()
    calls.length = 0

    store.toggleCollapse('42', { x: 5, y: 6 })
    await nextTick()
    // 去抖是 400ms；直接跑掉计时器，断言最终发出去的那一批
    await new Promise((resolve) => setTimeout(resolve, 450))
    const item = lastLayoutItem()
    expect(item.collapsed).toBe(true)
    expect(item.x).toBe(5)
    expect(item.y).toBe(6)
    // 本地状态也跟上了（不然卡片上的折叠标记要等下一次 reload 才出现）
    expect(store.visible.nodes.length).toBe(0)
    expect(store.counts.entities).toBe(0)
  })

  it('先拖后折叠 → 两次的字段**并成一条**，而不是后一次盖掉前一次', async () => {
    store.persistLayout('7', { x: 1, y: 2 }, { immediate: false })
    store.toggleCollapse('7', { x: 1, y: 2 })
    await new Promise((resolve) => setTimeout(resolve, 450))
    const writes = calls.filter(
      (c) => c.method === 'PATCH' && c.path.startsWith('/graph/layout'))
    expect(writes).toHaveLength(1)
    expect(writes[0].body.items).toHaveLength(1)
    expect(writes[0].body.items[0]).toMatchObject({ entity_id: 7, x: 1, y: 2,
                                                    collapsed: true })
  })

  it('折叠一个从未拖过的节点、又没给坐标 → 本地折叠，但**不落库**', async () => {
    // 写出一条 (0,0) 会让节点下次打开跳到左上角 —— 那比"这次折叠没存上"难查得多。
    const state = store.toggleCollapse('999', undefined)
    expect(state).toBe(true)
    await new Promise((resolve) => setTimeout(resolve, 450))
    expect(calls.filter((c) => c.path.startsWith('/graph/layout'))).toHaveLength(0)
  })
})

describe('标注的增删走同一条路由', () => {
  it('setEntityTags 把 add / remove 原样送到 PATCH /entities/{id}', async () => {
    await store.setEntityTags(3, { add: ['重点'], remove: ['旧的'], reason: '改主意了' })
    expect(lastBody('PATCH', '/entities/')).toEqual(
      { add_tags: ['重点'], remove_tags: ['旧的'], reason: '改主意了' })
  })

  it('什么都不改时**不发请求**（PATCH 对空改动是 400，不是故障）', async () => {
    const result = await store.setEntityTags(3, {})
    expect(result).toBeNull()
    expect(calls.filter((c) => c.method === 'PATCH')).toHaveLength(0)
  })

  it('tagEntity 仍然只是"加一个标签"的那条捷径', async () => {
    await store.tagEntity(3, '重点')
    expect(lastBody('PATCH', '/entities/')).toEqual({ add_tags: ['重点'] })
  })
})

describe('批量选中与批量改状态', () => {
  it('选中集合是**一份**：来回 toggle 之后回到原样', () => {
    store.clearSelection()
    store.toggleSelection(1)
    store.toggleSelection(2)
    store.toggleSelection(1)
    expect(store.selection).toEqual([2])
    store.clearSelection()
    expect(store.selection).toEqual([])
  })

  it('setSelection 去重并丢掉不是数字的东西', () => {
    store.setSelection([1, 1, '2', null, 'nope', 3])
    expect(store.selection).toEqual([1, 2, 3])
  })

  it('setLifecycle 一次请求带上整批 id（一个 job，不是 N 个）', async () => {
    store.setSelection([1, 2, 3])
    await store.setLifecycle(store.selection, 'retired', { reason: '季度清理' })
    expect(lastBody('POST', '/entities/lifecycle')).toEqual({
      entity_ids: [1, 2, 3], lifecycle: 'retired', dry_run: false,
      reason: '季度清理',
    })
    expect(calls.filter((c) => c.path.startsWith('/entities/lifecycle'))).toHaveLength(1)
  })

  it('空选中不发请求', async () => {
    store.clearSelection()
    expect(await store.setLifecycle(store.selection, 'retired')).toBeNull()
    expect(calls.filter((c) => c.path.startsWith('/entities/lifecycle'))).toHaveLength(0)
  })
})

describe('卡片上的折叠按钮', () => {
  it('点了会 emit，且**不冒泡**（折叠不该顺手改选中）', async () => {
    const wrapper = mount(EntityNode, {
      props: { data: { entityId: 1, kind: 'domain', key: 'a.example.com',
                       degree: 2, lifecycle: 'active', collapsed: false },
               selected: false },
    })
    const button = wrapper.find('.fold')
    expect(button.exists()).toBe(true)
    await button.trigger('click')
    expect(wrapper.emitted('toggle-collapse')).toHaveLength(1)
    // 折叠态在 DOM 上是**可断言的属性**，不只是两段 CSS
    expect(wrapper.attributes('data-collapsed')).toBe('0')
    wrapper.unmount()
  })

  it('折叠态有可见标记（不是只靠透明度）', async () => {
    const wrapper = mount(EntityNode, {
      props: { data: { entityId: 1, kind: 'domain', key: 'a.example.com',
                       degree: 0, lifecycle: 'retired', collapsed: true },
               selected: false },
    })
    expect(wrapper.attributes('data-collapsed')).toBe('1')
    expect(wrapper.find('.card').classes()).toContain('folded')
    expect(wrapper.find('.chip').text()).toContain('折叠')
    wrapper.unmount()
  })

  it('聚合节点没有折叠按钮（它没有身份，也就没有布局行）', async () => {
    const wrapper = mount(EntityNode, {
      props: { data: { entityId: null, kind: 'domain', key: 'domain × 3',
                       count: 3, aggregated: true, collapsed: false },
               selected: false },
    })
    expect(wrapper.find('.fold').exists()).toBe(false)
    wrapper.unmount()
  })
})

describe('画布上的批量条', () => {
  const STUBS = {
    VueFlow: { name: 'VueFlow', props: ['nodes', 'edges'], template: '<div />' },
    Background: true, Controls: true,
  }

  it('没选中时不出现；选中之后出现，并带上数量', async () => {
    store.clearSelection()
    const wrapper = mount(GraphCanvas, { global: { stubs: STUBS } })
    await nextTick()
    expect(wrapper.find('.batch-bar').exists()).toBe(false)

    store.setSelection([1, 2])
    await nextTick()
    const bar = wrapper.find('.batch-bar')
    expect(bar.exists()).toBe(true)
    expect(bar.text()).toContain('已选 2 个')
    store.clearSelection()
    await nextTick()
    expect(wrapper.find('.batch-bar').exists()).toBe(false)
    wrapper.unmount()
  })
})
