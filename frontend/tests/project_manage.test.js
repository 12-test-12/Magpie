/**
 * 项目管理（0.20.0 · 计划 F）的前端三件事：
 *
 *   1. **启动指针**：多个活跃项目时选第一个（原来是 `=== 1`，两个项目启动
 *      直接给画布喂一个 400 红错）；0 个活跃项目时不发 `/graph`、不报红 ——
 *      空态（EmptyGuide 建项目分支）接管，而不是把"还没建项目"渲染成故障。
 *   2. **归档 / 删除后的指针维护**：当前项目从活跃清单里消失 → 指到剩下的
 *      第一个；删除的 `confirm` 必须逐字（且 URL 编码）进 DELETE 查询串。
 *   3. **项目管理对话框**：清单带归档（恢复的前提）、归档行不给「切换」
 *      （切进去顶栏下拉里没有它，选中框会是空白）、删除要逐字输对名字。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

beforeEach(() => {
  vi.resetModules()
})

const EMPTY_GRAPH = { nodes: [], edges: [], layout: {}, project_id: 1, total: 0 }

const ACTIVE = (id, name) => ({
  id, name, status: 'active',
  counts: { entities: 0, observations: 0, relations: 0 },
})

/**
 * 一个可变的"迷你服务端"：`state.active` / `state.archived` 随归档 / 删除
 * 演化，URL 按方法分发。返回的 `calls` 供断言"请求真的发出去了、带着什么"。
 */
function serve(state) {
  const calls = []
  global.fetch = vi.fn(async (url, init = {}) => {
    const href = String(url)
    const method = ((init && init.method) || 'GET').toUpperCase()
    calls.push({ href, method })
    const reply = (data) => ({
      ok: true, status: 200,
      json: async () => ({ ok: true, data, meta: {} }),
    })
    const fail = (status, code, message) => ({
      ok: false, status,
      json: async () => ({ ok: false, error: { code, message } }),
    })
    const all = () => state.archived.concat(state.active)
    const find = (id) => all().find((row) => row.id === id)

    const archive = href.match(/\/projects\/(\d+)\/archive$/)
    if (method === 'POST' && archive) {
      const row = find(Number(archive[1]))
      if (!row) return fail(404, 'E_PROJECT_NOT_FOUND', '项目不存在')
      state.archived = state.archived.concat(
        state.active.filter((r) => r.id === row.id))
      state.active = state.active.filter((r) => r.id !== row.id)
      return reply({ id: row.id, name: row.name, status: 'archived', changed: true })
    }
    const restore = href.match(/\/projects\/(\d+)\/restore$/)
    if (method === 'POST' && restore) {
      const row = find(Number(restore[1]))
      if (!row) return fail(404, 'E_PROJECT_NOT_FOUND', '项目不存在')
      state.active = state.active.concat(
        state.archived.filter((r) => r.id === row.id))
      state.archived = state.archived.filter((r) => r.id !== row.id)
      return reply({ id: row.id, name: row.name, status: 'active', changed: true })
    }
    const remove = href.match(/\/projects\/(\d+)\?confirm=([^&]*)$/)
    if (method === 'DELETE' && remove) {
      const row = find(Number(remove[1]))
      if (!row) return fail(404, 'E_PROJECT_NOT_FOUND', '项目不存在')
      if (decodeURIComponent(remove[2]) !== row.name) {
        return fail(400, 'E_CONFIRM', 'confirm 必须逐字等于项目名')
      }
      state.active = state.active.filter((r) => r.id !== row.id)
      state.archived = state.archived.filter((r) => r.id !== row.id)
      return reply({ id: row.id, name: row.name, removed: { projects: 1 } })
    }
    if (method === 'GET' && href.includes('/projects')) {
      return reply(href.includes('include_archived') ? all() : state.active)
    }
    if (href.includes('/meta')) {
      return reply({ counts: { entities: 0, open_inbox: 0 }, kinds: [] })
    }
    if (href.includes('/graph')) return reply(EMPTY_GRAPH)
    if (href.includes('/domains/summary')) return reply([])
    if (href.includes('/tags')) return reply([])
    if (href.includes('/lenses/')) return reply({ lens: 'assets', kinds: [], total: 0 })
    throw new Error(`这个测试不该请求 ${href}`)
  })
  return calls
}

// ---------------------------------------------------------------------------
// 1. 启动指针
// ---------------------------------------------------------------------------

describe('启动时的项目指针', () => {
  it('两个活跃项目：选第一个，图带着它请求，不报红', async () => {
    const state = { active: [ACTIVE(1, '甲'), ACTIVE(2, '乙')], archived: [] }
    const calls = serve(state)
    const { store } = await import('../src/store.js')

    await store.reload()
    expect(store.project).toBe('甲')
    expect(store.loadError).toBe('')
    expect(calls.some(({ href }) => href.includes('/graph'))).toBe(false)
    store.setView('explore')
    await flushPromises()
    expect(calls.some(({ href }) => href.includes('/graph')
      && href.includes(`project=${encodeURIComponent('甲')}`))).toBe(true)
  })

  it('0 个活跃项目（只有归档）：不发 /graph 也不发 /lenses，loadError 空着', async () => {
    const state = { active: [], archived: [Object.assign(ACTIVE(9, '旧'), { status: 'archived' })] }
    const calls = serve(state)
    const { store } = await import('../src/store.js')

    await store.reload()
    expect(store.projects).toHaveLength(0)
    expect(store.project).toBe('')
    expect(calls.filter(({ href }) => href.includes('/graph'))).toHaveLength(0)
    expect(calls.filter(({ href }) => href.includes('/lenses/'))).toHaveLength(0)
    expect(store.loadError).toBe('')
  })
})

// ---------------------------------------------------------------------------
// 2. 归档 / 删除后的指针维护
// ---------------------------------------------------------------------------

describe('归档 / 删除后的指针维护', () => {
  it('归档当前项目 → 指到剩下的第一个，活跃清单不含它', async () => {
    const state = { active: [ACTIVE(1, '甲'), ACTIVE(2, '乙')], archived: [] }
    serve(state)
    const { store } = await import('../src/store.js')
    await store.reload()                              // 自动选中 甲
    expect(store.project).toBe('甲')

    await store.archiveProject(1)
    expect(store.project).toBe('乙')
    expect(store.projects.map((p) => p.name)).toEqual(['乙'])
  })

  it('归档最后一个项目 → 指针置空、/graph 不再请求、不报红', async () => {
    const state = { active: [ACTIVE(1, '甲')], archived: [] }
    const calls = serve(state)
    const { store } = await import('../src/store.js')
    await store.reload()
    calls.length = 0                                  // 只看归档之后的请求

    await store.archiveProject(1)
    expect(store.project).toBe('')
    expect(store.projects).toHaveLength(0)
    expect(calls.filter(({ href }) => href.includes('/graph'))).toHaveLength(0)
    expect(store.loadError).toBe('')
  })

  it('恢复归档项目回到活跃清单（当前为空时顺手选回第一个）', async () => {
    const state = { active: [], archived: [Object.assign(ACTIVE(9, '旧'), { status: 'archived' })] }
    serve(state)
    const { store } = await import('../src/store.js')
    await store.reload()
    expect(store.project).toBe('')                    // 只有归档 → 空态

    await store.restoreProject(9)
    expect(store.projects.map((p) => p.name)).toEqual(['旧'])
    expect(store.project).toBe('旧')                  // loadProjects 选回第一个
  })

  it('删除的 confirm 逐字（URL 编码）进查询串；删当前项目 → 指到剩下的', async () => {
    const state = { active: [ACTIVE(1, 'old name'), ACTIVE(2, '乙')], archived: [] }
    const calls = serve(state)
    const { store } = await import('../src/store.js')
    await store.reload()                              // 自动选中 old name
    calls.length = 0

    await store.deleteProject(1, 'old name')
    const removed = calls.find(({ method }) => method === 'DELETE')
    expect(removed.href).toContain('confirm=old%20name')
    expect(store.project).toBe('乙')
    expect(store.projects.map((p) => p.name)).toEqual(['乙'])
  })

  it('confirm 不对：400 原样抛给调用方，库一行没动', async () => {
    const state = { active: [ACTIVE(1, '甲'), ACTIVE(2, '乙')], archived: [] }
    serve(state)
    const { store } = await import('../src/store.js')
    await store.reload()

    await expect(store.deleteProject(1, 'typo')).rejects.toThrow(/E_CONFIRM/)
    expect(store.projects.map((p) => p.name)).toEqual(['甲', '乙'])
    expect(store.project).toBe('甲')                  // 指针没被失败的删除拽走
  })
})

// ---------------------------------------------------------------------------
// 3. 项目管理对话框 + 顶栏入口
// ---------------------------------------------------------------------------

describe('项目管理对话框', () => {
  it('清单带归档；归档行没有「切换」；删除要逐字输对名字才亮', async () => {
    const state = {
      active: [ACTIVE(1, '甲'), ACTIVE(2, '乙')],
      archived: [Object.assign(ACTIVE(9, '旧的'), { status: 'archived' })],
    }
    const calls = serve(state)
    const { store } = await import('../src/store.js')
    await store.reload()                              // 当前 = 甲

    const ProjectManager = (await import('../src/components/ProjectManager.vue')).default
    const wrapper = mount(ProjectManager)
    await flushPromises()

    // 清单带归档：3 行（活跃 2 + 归档 1），归档行没有「切换」
    const rowTexts = () => wrapper.findAll('tbody tr').map((tr) => tr.text())
    expect(rowTexts()).toHaveLength(3)
    const archivedRow = wrapper.findAll('tbody tr')
      .find((tr) => tr.text().includes('旧的'))
    expect(archivedRow.text()).toContain('已归档')
    expect(archivedRow.text()).toContain('恢复')
    expect(archivedRow.text()).not.toContain('切换')
    // 当前项目不给「切换」（切自己没有意义），但归档 / 删除照常
    const currentRow = wrapper.findAll('tbody tr')
      .find((tr) => tr.text().includes('甲'))
    expect(currentRow.text()).toContain('当前')
    expect(currentRow.text()).not.toContain('切换')

    // 删除流程：点删除 → 行内出确认输入 → 输不对 disabled → 输对才亮
    const buttons = (row, label) => row.findAll('button')
      .filter((button) => button.text() === label)
    await buttons(currentRow, '删除')[0].trigger('click')
    await flushPromises()
    const input = wrapper.find('input.confirm')
    expect(input.exists()).toBe(true)
    // 确认 UI 在被点删除的那一行里 —— 行序由服务端决定（归档在前），
    // 按"哪一行有确认输入"找，不按下标猜。
    const confirmRow = () => wrapper.findAll('tbody tr')
      .find((tr) => tr.find('input.confirm').exists())
    const confirmBtn = () => confirmRow().findAll('button')
      .find((button) => button.text().startsWith('确认删除'))
    expect(confirmBtn().attributes('disabled')).toBeDefined()
    await input.setValue('typo')
    await wrapper.vm.$nextTick()
    expect(confirmBtn().attributes('disabled')).toBeDefined()
    await input.setValue('甲')
    await wrapper.vm.$nextTick()
    expect(confirmBtn().attributes('disabled')).toBeUndefined()

    await confirmBtn().trigger('click')
    await flushPromises()
    const removed = calls.find(({ method }) => method === 'DELETE')
    expect(removed.href).toContain(`confirm=${encodeURIComponent('甲')}`)
    expect(rowTexts()).toHaveLength(2)                 // 清单刷新：甲没了
    wrapper.unmount()
  })

  it('顶栏「管理」按钮冒出 manage 事件（外壳用它开对话框）', async () => {
    serve({ active: [], archived: [] })
    const TopBar = (await import('../src/components/TopBar.vue')).default
    const wrapper = mount(TopBar)
    const manage = wrapper.findAll('button')
      .find((button) => button.text() === '管理')
    expect(manage).toBeTruthy()
    await manage.trigger('click')
    expect(wrapper.emitted('manage')).toHaveLength(1)
    wrapper.unmount()
  })
})
