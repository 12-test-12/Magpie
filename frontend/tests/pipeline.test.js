/**
 * 编排模式的**纯逻辑**（`src/pipeline.js`）。
 *
 * 这一层值得单独测，是因为"画布是受控组件"这条冻结项（F6）的可验证形式就是它：
 * 图完全由定义算出来 —— 同样的定义永远算出同样的节点与边，而所有的手势都只
 * 返回一份新定义。组件那一层（`PipelineCanvas.vue`）因此只剩下"把 props 递给
 * Vue Flow"和"把事件翻译成 store 调用"两件事。
 */

import { describe, expect, it } from 'vitest'

import {
  AUTHORITY_LEVELS, canConnect, canonical, definitionToFlow, dependencyEdges,
  emptyDefinition, reaches, SOURCE_KINDS, stepById, topoOrder, withAddedStep,
  withConnection, withDisconnection, withRemovedStep, withStepPatch,
} from '../src/pipeline.js'

function sample() {
  return {
    format_version: 1,
    name: '常规一轮',
    project: 'demo',
    base_dir: 'D:/Magpie/bench/e2e',
    data_dir: null,
    scope: null,
    steps: [
      { id: 'dns', source: { kind: 'glob', pattern: 'recon/subfinder.txt' },
        source_name: 'subfinder', parser: 'domainlist', after: [],
        authority: 'unverified', archive: 'archived', batch_size: 2000, dry_run: false },
      { id: 'probe', source: { kind: 'glob', pattern: 'recon/*httpx*.jsonl' },
        source_name: null, parser: null, after: ['dns'],
        authority: 'unverified', archive: 'archived', batch_size: 2000, dry_run: false },
    ],
  }
}

describe('canonical：dirty 判据的底座', () => {
  it('key 的顺序不影响结果（同一份定义不因为书写顺序变成两份）', () => {
    expect(canonical({ a: 1, b: { c: 2, d: 3 } }))
      .toBe(canonical({ b: { d: 3, c: 2 }, a: 1 }))
  })

  it('数组顺序**是**语义（after 的顺序不同就是不同的写法，但值不同要能看出来）', () => {
    expect(canonical([1, 2])).not.toBe(canonical([2, 1]))
  })

  it('null 与 undefined 归一到同一个值（缺字段不该让 dirty 永远亮着）', () => {
    expect(canonical({ a: null })).toBe(canonical({ a: null }))
    expect(canonical(null)).toBe(canonical(undefined))
  })
})

describe('依赖图完全来自定义', () => {
  it('dependencyEdges 只画 after 里的引用', () => {
    expect(dependencyEdges(sample())).toEqual([{ source: 'dns', target: 'probe' }])
  })

  it('引用一个不存在的步骤时**不画线**（画一条通向空气的边比不画更误导）', () => {
    const broken = sample()
    broken.steps[1].after = ['ghost']
    expect(dependencyEdges(broken)).toEqual([])
  })

  it('definitionToFlow 是纯函数：同样的定义算出同样的图', () => {
    expect(definitionToFlow(sample())).toEqual(definitionToFlow(sample()))
  })

  it('节点带上"这一步是什么"的全部信息，边带 after 标签', () => {
    const flow = definitionToFlow(sample(), { selectedId: 'probe' })
    expect(flow.nodes.map((n) => n.id)).toEqual(['dns', 'probe'])
    expect(flow.nodes[0].data.sourceName).toBe('subfinder')
    expect(flow.nodes[1].data.dependsOn).toEqual(['dns'])
    expect(flow.nodes[1].data.selected).toBe(true)
    expect(flow.nodes[0].data.selected).toBe(false)
    expect(flow.edges).toHaveLength(1)
    expect(flow.edges[0]).toMatchObject({ source: 'dns', target: 'probe' })
  })

  it('空定义算出空图（而不是 null —— 组件的 props 契约里没有"可能是 null"）', () => {
    expect(definitionToFlow(emptyDefinition('demo'))).toEqual({ nodes: [], edges: [] })
    expect(definitionToFlow(null)).toEqual({ nodes: [], edges: [] })
  })
})

describe('编辑动作是纯变换', () => {
  it('withStepPatch 返回新对象，且**改不掉 id**', () => {
    const before = sample()
    const after = withStepPatch(before, 'dns', { id: 'renamed', parser: 'nmap' })
    expect(after).not.toBe(before)
    expect(before.steps[0].parser).toBe('domainlist')          // 原定义没被就地改
    expect(stepById(after, 'dns').parser).toBe('nmap')
    expect(stepById(after, 'renamed')).toBe(null)
  })

  it('withAddedStep 建一个可编辑的空步骤，id 不撞号', () => {
    const next = withAddedStep(sample())
    expect(next.steps).toHaveLength(3)
    expect(next.steps[2].id).toBe('step1')
    expect(withAddedStep(next).steps[3].id).toBe('step2')
  })

  it('withRemovedStep 顺带清掉别人对它的引用', () => {
    // 不清的话，库里会留下一份 after 指向不存在步骤的定义 —— 数据层会拒绝它，
    // 于是"删一个步骤"变成"这条定义再也存不下去"。
    const next = withRemovedStep(sample(), 'dns')
    expect(next.steps.map((s) => s.id)).toEqual(['probe'])
    expect(next.steps[0].after).toEqual([])
  })

  it('withConnection / withDisconnection 只动 after 一个字段', () => {
    const connected = withConnection(sample(), 'probe', 'dns')  // 会成环 → 拒绝
    expect(dependencyEdges(connected)).toEqual([{ source: 'dns', target: 'probe' }])

    const reversed = withDisconnection(sample(), 'dns', 'probe')
    expect(reversed.steps[1].after).toEqual([])
    expect(reversed.steps[0]).toEqual(sample().steps[0])        // 别的字段一个字没动
  })

  it('连接已存在的依赖是 no-op（幂等，不写第二条边）', () => {
    const once = withConnection(sample(), 'dns', 'probe')
    expect(once.steps[1].after).toEqual(['dns'])
  })
})

describe('连线前的检查：成环在这里挡住', () => {
  it('自环 / 重复 / 成环 / 不存在的端点各有各的理由', () => {
    const definition = sample()
    expect(canConnect(definition, 'dns', 'dns').ok).toBe(false)
    expect(canConnect(definition, 'dns', 'probe').ok).toBe(false)   // 已经有了
    expect(canConnect(definition, 'probe', 'dns').ok).toBe(false)   // 会成环
    expect(canConnect(definition, 'dns', 'ghost').ok).toBe(false)
    expect(canConnect(emptyDefinition('demo'), 'a', 'b').ok).toBe(false)
    expect(canConnect(definition, 'dns', 'probe').why).toMatch(/已经依赖/)
  })

  it('reaches 只在有路的时候为真', () => {
    const definition = sample()
    expect(reaches(definition, 'dns', 'probe')).toBe(true)
    expect(reaches(definition, 'probe', 'dns')).toBe(false)
  })
})

describe('topoOrder：只用来摆位置', () => {
  it('被依赖的排在前面，且结果稳定', () => {
    expect(topoOrder(sample())).toEqual(['dns', 'probe'])
    const shuffled = sample()
    shuffled.steps.reverse()
    expect(topoOrder(shuffled)).toEqual(['dns', 'probe'])
  })

  it('有环时不丢步骤（界面不该因为一份坏定义就少画一个节点）', () => {
    const cyclic = sample()
    cyclic.steps[0].after = ['probe']
    expect(topoOrder(cyclic).sort()).toEqual(['dns', 'probe'])
  })
})

describe('界面上的枚举与数据层同源', () => {
  it('source.kind 的三个取值逐字对应 core 的 SOURCE_KINDS', () => {
    expect(SOURCE_KINDS.map((k) => k.value)).toEqual(['glob', 'file', 'dir'])
  })

  it('authority 的四个取值逐字对应 schema/001 的 CHECK', () => {
    expect(AUTHORITY_LEVELS).toEqual(['unverified', 'semiautomatic', 'manual', 'verified'])
  })
})
