/**
 * 卡片折叠按钮的**事件链路**测试：EntityNode 发 → GraphCanvas 收 → store 改。
 *
 * 这条链路坏过很久而且没有任何测试红：Vue Flow 渲染自定义节点时只传固定的
 * 上下文 props、**不透传组件上的监听器**，于是 `<VueFlow @toggle-collapse>`
 * 收到的永远是空 —— 按钮是个摆设。开发时数据少、很少点它，症状"看起来像
 * 这个功能本来就没有"。
 *
 * 现在事件是卡片根元素上**冒泡的 DOM CustomEvent**，画布用程序化
 * `addEventListener` 收（带连字符的事件名走模板绑定还有 Vue 编译歧义）。
 * 这两层各钉一条：发得出来、收得到。
 */

import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'

import { store } from '../src/store.js'

describe('折叠按钮的事件链路', () => {
  it('EntityNode：点折叠按钮，从卡片根元素冒泡出 toggle-collapse', async () => {
    const EntityNode = (await import('../src/components/EntityNode.vue')).default
    const wrapper = mount(EntityNode, {
      props: { data: { entityId: 7, kind: 'domain', key: 'a.example.com',
                       degree: 0, lifecycle: 'active', collapsed: false },
               selected: false },
    })
    const seen = []
    wrapper.element.addEventListener('toggle-collapse', (e) => seen.push(e))
    await wrapper.find('.fold').trigger('click')
    expect(seen).toHaveLength(1)
    expect(seen[0]).toBeInstanceOf(CustomEvent)
    wrapper.unmount()
  })

  it('GraphCanvas：收下冒泡事件，带 id 交给 store.toggleCollapse', async () => {
    const GraphCanvas = (await import('../src/components/GraphCanvas.vue')).default
    const wrapper = mount(GraphCanvas, {
      global: {
        stubs: {
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

    const spy = vi.spyOn(store, 'toggleCollapse').mockReturnValue(true)
    const card = document.createElement('div')
    card.setAttribute('data-entity-id', '42')
    wrapper.find('.canvas').element.appendChild(card)
    card.dispatchEvent(new CustomEvent('toggle-collapse', { bubbles: true }))

    expect(spy).toHaveBeenCalledTimes(1)
    expect(String(spy.mock.calls[0][0])).toBe('42')
    spy.mockRestore()
    wrapper.unmount()
  })

  it('GraphCanvas：没有 data-entity-id 的目标不理会（聚合卡/意外冒泡）', async () => {
    const GraphCanvas = (await import('../src/components/GraphCanvas.vue')).default
    const wrapper = mount(GraphCanvas, {
      global: {
        stubs: {
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

    const spy = vi.spyOn(store, 'toggleCollapse').mockReturnValue(true)
    const bare = document.createElement('div')
    wrapper.find('.canvas').element.appendChild(bare)
    bare.dispatchEvent(new CustomEvent('toggle-collapse', { bubbles: true }))
    expect(spy).not.toHaveBeenCalled()

    const agg = document.createElement('div')
    agg.setAttribute('data-entity-id', 'null')
    wrapper.find('.canvas').element.appendChild(agg)
    agg.dispatchEvent(new CustomEvent('toggle-collapse', { bubbles: true }))
    expect(spy).not.toHaveBeenCalled()

    spy.mockRestore()
    wrapper.unmount()
  })
})
