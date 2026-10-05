/**
 * 左栏两处分组（评审五/六）：
 *
 *   导航四组 —— 攻击面 / 安全 / 分析 / 任务。**页清单仍然只有 `store.VIEWS`
 *               一份**（外加 VIEWS 没登记、但库里真有的镜头页 api/traffic/graph
 *               —— 漏掉它们就是"库里有 endpoint 9,593、导航里没有接口这一页"）。
 *               这里的分组是展示元数据，钉它是因为分组错了顶多难找、而**页面
 *               从导航里静默消失**才是真坏 —— 后者正是未登记镜头的老病。
 *   资产规模两子标 —— 攻击面 / 安全（`SECURITY_KINDS = {secret, vuln}`）。
 *               组内各自降序，比例条分母仍是**全体**最大值（跨组同一把尺，
 *               Security 组不会因为组内最大而虚胖）。
 *
 * 数据用 `store.lensKeys` / `store.lenses` 直接喂（与 domain_view 同款）——
 * 镜头清单是库推导的，这里不模拟 `/meta`，只模拟"库已经答完了"的状态。
 * 不 `resetModules`：组件与 store 在文件顶部同一个注册表里取好，换注册表
 * 会让喂进 store 的状态跑到组件看不见的另一份单例上。
 */

import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../src/api.js', () => ({
  API_PREFIX: '/api/v1',
  token: { value: 'test' },
  hasToken: () => true,
  ApiError: class ApiError extends Error {},
  get: vi.fn(async () => ({})),
  post: vi.fn(async () => ({})),
  patch: vi.fn(async () => ({})),
  put: vi.fn(async () => ({})),
  watchRun: vi.fn(() => () => {}),
  watchImport: vi.fn(() => () => {}),
}))

const store = (await import('../src/store.js')).store
const LeftPanel = (await import('../src/components/LeftPanel.vue')).default

/** 六个镜头全部喂上 —— 覆盖 VIEWS 登记过的（assets/vuln…）与没登记的
 *  （api/traffic/graph），后三个走的是"lensRows 有、VIEWS 没有"那条分支。 */
function feedLenses() {
  store.lensKeys = ['assets', 'services', 'api', 'traffic', 'vuln', 'graph']
  store.lenses.assets = { total: 9593, kinds: [
    { kind: 'domain', count: 9000 }, { kind: 'ip', count: 400 }] }
  store.lenses.services = { total: 520, kinds: [
    { kind: 'service', count: 500 }, { kind: 'product', count: 20 }] }
  store.lenses.api = { total: 150, kinds: [{ kind: 'endpoint', count: 150 }] }
  store.lenses.traffic = { total: 999, kinds: [{ kind: 'flow', count: 999 }] }
  store.lenses.vuln = { total: 13, kinds: [{ kind: 'vuln', count: 13 }] }
  store.lenses.graph = { total: 7, kinds: [{ kind: 'secret', count: 7 }] }
}

function mountPanel() {
  feedLenses()
  const wrapper = mount(LeftPanel)
  wrapper.vm.$nextTick()
  return wrapper
}

/** 按 h2 标题取 section（导航组 / 资产规模 / 管线都在 section 里）。 */
function sectionByTitle(wrapper, title) {
  return wrapper.findAll('section').find((section) => {
    const h2 = section.find('h2.section')
    return h2.exists() && h2.text() === title
  })
}

beforeEach(() => {
  store.filter.kinds = []
  store.filter.underDomain = null
  store.filter.keyword = ''
  store.view = 'overview'
})

describe('导航四组（评审五）', () => {
  it('四个组标题按 攻击面 / 安全 / 分析 / 任务 出现在资产规模之前', () => {
    const wrapper = mountPanel()

    const titles = wrapper.findAll('h2.section').map((h) => h.text())
    expect(titles).toEqual([
      '攻击面', '安全', '分析', '任务',
      '资产规模（点击筛选画布）', '管线',
    ])
    wrapper.unmount()
  })

  it('组内成员各归其位：未登记的镜头页也不消失', () => {
    const wrapper = mountPanel()

    const attack = sectionByTitle(wrapper, '攻击面')
    const attackText = attack.text()
    for (const label of ['攻击面画布', '资产', '服务与中间件', '接口', '图谱']) {
      expect(attackText).toContain(label)     // api/graph 是 VIEWS 没登记的镜头页
    }

    const security = sectionByTitle(wrapper, '安全')
    for (const label of ['漏洞', '风险', 'Inbox']) {
      expect(security.text()).toContain(label)
    }

    const analysis = sectionByTitle(wrapper, '分析')
    for (const label of ['数据体检', '流量']) {
      expect(analysis.text()).toContain(label)
    }

    const tasks = sectionByTitle(wrapper, '任务')
    for (const label of ['编排', '作业历史']) {
      expect(tasks.text()).toContain(label)
    }

    // 总览是左栏顶部那一行，不进导航组（text() 不含 title 属性，正好）
    expect(attackText).not.toContain('项目总览')
    wrapper.unmount()
  })

  it('镜头徽标显示库里的数（fmtInt 千分位），不是画布载入数', () => {
    const wrapper = mountPanel()
    const attack = sectionByTitle(wrapper, '攻击面')
    expect(attack.text()).toContain('9,593')       // assets.total
    wrapper.unmount()
  })

  it('点导航项真的换页（setView），组归属不影响页面可达', async () => {
    const wrapper = mountPanel()
    const analysis = sectionByTitle(wrapper, '分析')
    const doctor = analysis.findAll('li')
      .find((li) => li.text().includes('数据体检'))
    await doctor.trigger('click')
    expect(store.view).toBe('doctor')
    wrapper.unmount()
  })
})

describe('资产规模两子标（评审六）', () => {
  it('secret/vuln 进「安全」子标，其余进「攻击面」；组内各自降序', () => {
    const wrapper = mountPanel()

    const groups = wrapper.findAll('.kind-group')
    expect(groups).toHaveLength(2)
    expect(wrapper.findAll('.group-label').map((el) => el.text()))
      .toEqual(['攻击面', '安全'])

    const attackNames = groups[0].findAll('.krow .name').map((el) => el.text())
    expect(attackNames).toContain('domain')
    expect(attackNames).toContain('endpoint')
    expect(attackNames).not.toContain('secret')
    expect(attackNames).not.toContain('vuln')
    // 组内降序：domain 9000 → flow 999 → service 500 → ip 400 → …
    expect(attackNames).toEqual(['domain', 'flow', 'service', 'ip',
                                  'endpoint', 'product'])

    const securityNames = groups[1].findAll('.krow .name').map((el) => el.text())
    expect(securityNames).toEqual(['vuln', 'secret'])   // 13 > 7，组内降序
    wrapper.unmount()
  })

  it('比例条分母是全体最大值 —— Security 组不因组内最大而虚胖', () => {
    const wrapper = mountPanel()

    const widthOf = (el) => {
      const m = /width:\s*([\d.]+)%/.exec(el.attributes('style') || '')
      return m ? Number(m[1]) : 0
    }
    const groups = wrapper.findAll('.kind-group')
    const domainBar = groups[0].find('.krow .bar i')   // 9000/9000 = 100%
    const vulnBar = groups[1].find('.krow .bar i')     // 13/9000 → 保底 10%
    expect(widthOf(domainBar)).toBe(100)
    expect(widthOf(vulnBar)).toBeLessThan(widthOf(domainBar))
    expect(widthOf(vulnBar)).toBeGreaterThan(0)         // 非零保底
    wrapper.unmount()
  })

  it('点击行为没变：仍走 toggleKind（组只是呈现，不是新语义）', () => {
    const wrapper = mountPanel()
    // 不真点（会触发 loadGraph 走网络）——title 的措辞是 toggleKind 专属
    // （"只看画布上的 <kind>"），换语义的话这句承诺会先烂。
    const groups = wrapper.findAll('.kind-group')
    expect(groups[0].find('.krow').attributes('title'))
      .toContain('只看画布上的 domain')
    expect(groups[1].find('.krow').attributes('title'))
      .toContain('只看画布上的 vuln')
    wrapper.unmount()
  })
})
