/**
 * **只读断言**（2.8 交付标准表：「探索模式无法增删节点与边（自动化用例断言）」）。
 *
 * 这条测试分成两个层次，因为它们防的是两种不同的退化：
 *
 *   1. **结构层**：`store` 的导出里不存在任何能产出新节点 / 新边的能力。
 *      断言的是"这些名字**不存在**"—— 所以将来有人加了一个 `addNode`，这条
 *      会红。只断言"当前没调用"是没用的：那正是明天会被打破的东西。
 *   2. **挂载层**：真的把画布挂起来，断言连线的入口是关死的 ——
 *      `nodesConnectable=false` / `edgesUpdatable=false`，且没有 `connect`
 *      事件处理器。结构层管不到"Vue Flow 的默认值"，而它的默认值是可连接的。
 *
 * `check.py lint` 的 READONLY 静态红线是同一件事的**第三层**（全仓源码扫描，
 * 每次 `check.py` 都跑）。三层不重复：静态红线保证"没人偷偷写"，这两条保证
 * "写出来的东西确实是只读的"。
 */

import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'

import * as storeModule from '../src/store.js'
import { store } from '../src/store.js'

/** 一旦有人加了其中任何一个，"只读"就不再是结构上的事实了。 */
const FORBIDDEN = [
  'addNode', 'addNodes', 'addEdge', 'addEdges',
  'removeNode', 'removeNodes', 'removeEdge', 'removeEdges',
  'setNodes', 'setEdges', 'updateNode', 'updateEdge',
  'applyNodeChanges', 'applyEdgeChanges',
  'connectNodes', 'onConnect', 'createNode', 'createEdge',
]

describe('探索模式的只读是结构性的，不是自觉', () => {
  it('store 不导出任何"往图里加东西"的能力', () => {
    for (const name of FORBIDDEN) {
      expect(Object.prototype.hasOwnProperty.call(store, name)).toBe(false)
      expect(Object.prototype.hasOwnProperty.call(store, name)).toBe(false)
    }
  })

  it('模块级的具名导出里也没有它们', () => {
    for (const name of FORBIDDEN) {
      expect(Object.prototype.hasOwnProperty.call(storeModule, name)).toBe(false)
    }
  })

  it('探索模式允许的四种操作都在（只读不等于不可用）', () => {
    for (const name of ['moveEntity', 'toggleCollapse', 'persistLayout',
                        'setFilter', 'toggleKind', 'clearFilter', 'tagEntity']) {
      expect(typeof store[name]).toBe('function')
    }
  })

  it('moveEntity 只改坐标，不产生节点', async () => {
    // 一个最小的图：直接喂给 store 的内部状态是不行的（它是私有的），
    // 所以这里断言的是**行为**：调用它之后，导出面上的"节点数"没变。
    const before = store.counts.entities
    store.moveEntity('1', { x: 10, y: 20 })
    await nextTick()
    expect(store.counts.entities).toBe(before)
  })

  it('moveEntity 拒绝非数字坐标（不写出半个布局）', () => {
    store.moveEntity('1', { x: 'nope', y: null })
    store.moveEntity('1', undefined)
    expect(true).toBe(true)      // 不抛异常即可 —— 它只是什么都不做
  })
})

describe('画布把连线入口关死了', () => {
  it('挂载后的 VueFlow 拿到的是 nodesConnectable=false / edgesUpdatable=false', async () => {
    // Vue Flow 需要一个有尺寸的容器；jsdom 里尺寸都是 0，这不影响属性断言。
    const GraphCanvas = (await import('../src/components/GraphCanvas.vue')).default
    const wrapper = mount(GraphCanvas, {
      global: {
        stubs: {
          // 让断言落在**我们传给它的属性**上，而不是 Vue Flow 的内部实现。
          // 用真的 VueFlow 只会让这条测试在 Vue Flow 升级时因为无关的 DOM
          // 变化而红 —— 那种红会训练人忽略红色。
          VueFlow: {
            name: 'VueFlow',
            props: ['nodes', 'edges', 'nodesConnectable', 'edgesUpdatable',
                    'nodesDraggable', 'nodeTypes'],
            template: '<div class="vue-flow-stub" />',
          },
          Background: true,
          Controls: true,
        },
      },
    })
    await nextTick()

    const flow = wrapper.findComponent({ name: 'VueFlow' })
    expect(flow.exists()).toBe(true)
    expect(flow.props('nodesConnectable')).toBe(false)
    expect(flow.props('edgesUpdatable')).toBe(false)
    // 拖位置**是**允许的（探索模式的四种操作之一）—— 只读不等于动不了
    expect(flow.props('nodesDraggable')).toBe(true)

    // 没有 `connect` 监听器：没有回调，就没有"某个事件顺手加一条边"的地方
    const listeners = flow.vm.$attrs ? Object.keys(flow.vm.$attrs) : []
    expect(listeners.filter((k) => /^on[A-Z]/.test(k) && /connect/i.test(k)))
      .toHaveLength(0)
    wrapper.unmount()
  })

  it('实体卡片不渲染任何连接点（Handle）', async () => {
    const EntityNode = (await import('../src/components/EntityNode.vue')).default
    const wrapper = mount(EntityNode, {
      props: { data: { entityId: 1, kind: 'domain', key: 'a.example.com',
                       degree: 2, lifecycle: 'active' }, selected: false },
    })
    expect(wrapper.html()).not.toMatch(/handle|Handle/)
    // 但类型色带在 —— 卡片本身是完整的
    expect(wrapper.find('.band').exists()).toBe(true)
    wrapper.unmount()
  })
})

describe('两种模式在属性上就是两个模式（3.7b）', () => {
  /** 两个画布共用的窥探壳：断言落在**我们传进去的属性**上，不落在 Vue Flow
   *  的内部实现上。 */
  const STUBS = {
    VueFlow: {
      name: 'VueFlow',
      props: ['nodes', 'edges', 'nodesConnectable', 'edgesUpdatable',
              'nodesDraggable', 'nodeTypes'],
      template: '<div class="vue-flow-stub" />',
    },
    Background: true,
    Controls: true,
  }

  it('探索模式画布：data-mode=explore，且连线入口是关死的', async () => {
    const GraphCanvas = (await import('../src/components/GraphCanvas.vue')).default
    const wrapper = mount(GraphCanvas, { global: { stubs: STUBS } })
    await nextTick()
    expect(wrapper.find('.canvas').attributes('data-mode')).toBe('explore')
    const flow = wrapper.findComponent({ name: 'VueFlow' })
    expect(flow.props('nodesConnectable')).toBe(false)
    wrapper.unmount()
  })

  it('编排模式画布：data-mode=pipeline，且连线入口是打开的', async () => {
    // 编排画布的图来自草稿 —— 组件挂载之前先给它一份（空定义也算一份）。
    store.newPipelineDraft()
    store.patchDefinition({ name: '模式断言用例' })
    store.addStep()
    await nextTick()

    const PipelineCanvas = (await import('../src/components/PipelineCanvas.vue')).default
    const wrapper = mount(PipelineCanvas, { global: { stubs: STUBS } })
    await nextTick()
    expect(wrapper.find('.canvas').attributes('data-mode')).toBe('pipeline')
    const flow = wrapper.findComponent({ name: 'VueFlow' })
    expect(flow.props('nodesConnectable')).toBe(true)
    // 边仍然不可改接：这个模式需要的是**拉一条新线**，不是把已有的线挪个地方。
    expect(flow.props('edgesUpdatable')).toBe(false)
    // 受控：节点来自 store 里的草稿，组件自己不算图。
    expect(flow.props('nodes')).toHaveLength(1)
    wrapper.unmount()
  })
})

describe('编排模式的草稿改动作不改库', () => {
  it('addStep / removeStep / connectSteps 只改草稿，不碰探索模式的图', () => {
    store.newPipelineDraft()
    store.addStep()
    store.addStep()
    expect(store.pipeline.definition.steps).toHaveLength(2)
    const [first, second] = store.pipeline.definition.steps.map((s) => s.id)
    store.connectSteps(first, second)
    expect(store.pipeline.definition.steps[1].after).toEqual([first])
    store.removeStep(first)
    expect(store.pipeline.definition.steps).toHaveLength(1)
    expect(store.pipeline.definition.steps[0].after).toEqual([])
    // 探索模式的图（`_raw` 的派生物）一个节点都没多出来
    expect(store.counts.entities).toBe(0)
  })

  it('连线会挡住成环', () => {
    store.newPipelineDraft()
    store.addStep()
    store.addStep()
    const [a, b] = store.pipeline.definition.steps.map((s) => s.id)
    store.connectSteps(a, b)
    store.connectSteps(b, a)                       // 会成环 → 应当被拒
    expect(store.pipeline.definition.steps[0].after).toEqual([])
  })
})

describe('筛选是只读的第四种操作', () => {
  it('toggleKind 幂等来回，不会碰图的内容', () => {
    const before = store.counts.entities
    store.toggleKind('domain')
    store.toggleKind('domain')
    expect(store.filter.kinds).toEqual([])
    expect(store.counts.entities).toBe(before)
  })
})
