/**
 * 风险路径（评审九）：右栏「计算风险路径」—— Secret → … → Domain 这条链。
 *
 * 四条纪律逐个钉死：
 *
 *   1. **选中态一个请求都不发**：按钮触发是硬要求（agent_context 的 get ×1
 *      钉子守着"复制不附带别的请求"，自动拉邻居会把它打红，而它红的原因与
 *      复制毫无关系；depth=5 在大库上也不该为每个被点开的实体都付一次）。
 *   2. **端点与参数钉死**：`/graph/{id}/neighbors?depth=5&limit=500`，一次
 *      请求，客户端在返回的子图上走 BFS —— 不改后端、不加端点。
 *   3. **链渲染且可点**：每个 hop 是一枚 chip，点了走 `store.ensureVisible`
 *      （这里 spy 掉 —— 真实现会再打两个请求，与"这次点击只花一次请求"的
 *      断言无关）。
 *   4. **走不到要明说**：5 跳里没有目标是"关系数据还不够连通"，不是空白 ——
 *      静默的空区块会让人以为按钮坏了。
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

//: 起点是 vuln（非 domain）→ 目标是 domain。三跳：vuln → ip → domain。
const REACHABLE = {
  nodes: [
    { id: 42, kind: 'vuln', entity_key: 'CVE-2021-44228' },
    { id: 7, kind: 'ip', entity_key: '203.0.113.9' },
    { id: 1, kind: 'domain', entity_key: 'victim.example.com' },
  ],
  edges: [
    { src: 42, dst: 7, rel: 'found_on' },
    { src: 7, dst: 1, rel: 'resolves_to' },
  ],
}

//: 走不到：5 跳里没有任何 domain。
const UNREACHABLE = {
  nodes: [
    { id: 42, kind: 'vuln', entity_key: 'CVE-2021-44228' },
    { id: 7, kind: 'ip', entity_key: '203.0.113.9' },
  ],
  edges: [{ src: 42, dst: 7, rel: 'found_on' }],
}

const DETAIL = {
  entity: {
    id: 42, entity_key: 'CVE-2021-44228', kind: 'vuln', lifecycle: 'active',
    first_seen: 'T1', last_seen: 'T2',
  },
  observations: [],
  relations: [],
  tags: [],
  manual: { protected_fields: [] },
  open_inbox: [],
  timeline: [],
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
  // 初值是 ''（store._project），显式钉住 —— 端点断言要的是不带 project 的
  // 那条 URL；万一别的用例改过 project，这里不背锅。
  store.project = ''
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('风险路径（评审九）', () => {
  it('选中实体本身不发任何请求 —— 按钮是唯一触发器', async () => {
    const wrapper = await mountPanel()
    await flush()

    expect(get).not.toHaveBeenCalled()
    expect(wrapper.find('.path-btn').exists()).toBe(true)
    wrapper.unmount()
  })

  it('点按钮 → 钉死的 neighbors URL → 链渲染且每跳可点', async () => {
    get.mockResolvedValue(REACHABLE)
    const ensure = vi.spyOn(store, 'ensureVisible').mockImplementation(() => {})
    const wrapper = await mountPanel()
    await flush()
    expect(get).not.toHaveBeenCalled()                  // 挂载本身零请求

    await wrapper.find('.path-btn').trigger('click')
    await flush()

    // 端点钉死：一次 depth=5&limit=500，project 空时没有多余的参数
    expect(get).toHaveBeenCalledTimes(1)
    expect(get).toHaveBeenCalledWith('/graph/42/neighbors?depth=5&limit=500')

    const chain = wrapper.find('.chain')
    expect(chain.exists()).toBe(true)
    const hops = wrapper.findAll('.chain-node')
    expect(hops).toHaveLength(3)
    // 链文本 = 起点 → 中转 → 目标，关系标在段上
    const chainText = chain.text()
    expect(chainText).toContain('CVE-2021-44228')
    expect(chainText).toContain('203.0.113.9')
    expect(chainText).toContain('victim.example.com')
    expect(chainText).toContain('found_on')
    expect(chainText).toContain('resolves_to')

    // 每跳可点 → store.ensureVisible（spy 掉了真实现的后续请求）
    await hops[2].trigger('click')
    expect(ensure).toHaveBeenCalledWith(1)
    wrapper.unmount()
  })

  it('目标随起点反转：domain 的目标是最近的 secret / vuln（提示文案钉死）', async () => {
    const wrapper = await mountPanel({
      ...DETAIL,
      entity: { ...DETAIL.entity, kind: 'domain', entity_key: 'victim.example.com' },
    })

    expect(wrapper.text()).toContain('走到最近的 secret / vuln')
    wrapper.unmount()
  })

  it('5 跳里走不到：明说「关系数据还不够」，不留空白', async () => {
    get.mockResolvedValue(UNREACHABLE)
    const wrapper = await mountPanel()
    await flush()

    await wrapper.find('.path-btn').trigger('click')
    await flush()

    expect(wrapper.find('.chain').exists()).toBe(false)
    expect(wrapper.text()).toContain('5 跳邻居里没有走到目标')
    // 走不到也是一次请求换一个答案 —— 按钮回到可点（idle），没有错误文案
    expect(wrapper.find('.path-btn').text()).toContain('计算风险路径')
    expect(wrapper.text()).not.toContain('风险路径计算失败')
    wrapper.unmount()
  })

  it('请求失败：错误红着说出来，状态回 idle（按钮可再试）', async () => {
    get.mockRejectedValueOnce(new Error('E_DB: 库炸了'))
    const wrapper = await mountPanel()
    await flush()

    await wrapper.find('.path-btn').trigger('click')
    await flush()

    expect(wrapper.text()).toContain('风险路径计算失败')
    expect(wrapper.text()).toContain('E_DB')
    expect(wrapper.find('.path-btn').text()).toContain('计算风险路径')
    expect(wrapper.find('.path-btn').attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })
})
