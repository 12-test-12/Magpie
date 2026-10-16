import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const response = data => ({ ok: true, status: 200, json: async () => ({ ok: true, data }) })
let store, View
const groups = { rows: [{ group_key: '@a.example.com', label: 'a.example.com', count: 1250 }], total: 1, entity_total: 1250, limit: 50, next_cursor: null }
const rows = { rows: [{ id: 1, kind: 'endpoint', entity_key: 'a.example.com/api/v1/users', observed: [{semantic_field: 'method', value: 'GET'}] }], total: 1250, limit: 50, next_cursor: 'next' }
function backend(overrides = () => null) {
  global.fetch = vi.fn(async (url, options) => {
    const custom = overrides(String(url), options)
    if (custom) return custom
    if (String(url).includes('/workspace/entities/')) return response({entity: rows.rows[0], rows: [], total: 0, next_cursor: null})
    const query = new URL(String(url), 'http://local').searchParams
    return response(query.get('mode') === 'list' ? rows : groups)
  })
}
beforeEach(async () => {
  vi.resetModules()
  window.history.replaceState({}, '', '/?token=test')
  store = (await import('../src/store.js')).store
  store.project = 'demo'
  View = (await import('../src/views/WorkspaceView.vue')).default
})

describe('bounded workspace navigation', () => {
  it('groups → paths → bounded rows → detail, back restores the parent', async () => {
    backend()
    const wrapper = mount(View, { props: {category: 'api'} })
    await flushPromises()
    expect(wrapper.text()).toContain('1,250')
    expect(wrapper.findAll('.workspace-table tbody tr')).toHaveLength(0)
    await wrapper.find('.group-card').trigger('click'); await flushPromises()
    expect(global.fetch.mock.calls.at(-1)[0]).toContain('mode=path')
    await wrapper.find('.group-card').trigger('click'); await flushPromises()
    expect(wrapper.findAll('.workspace-table tbody tr')).toHaveLength(1)
    expect(wrapper.text()).toContain('GET')
    await wrapper.find('.row-link').trigger('click'); await flushPromises()
    expect(wrapper.find('.workspace-detail').exists()).toBe(true)
    await wrapper.find('[aria-label="关闭详情"]').trigger('click')
    expect(wrapper.find('.workspace-detail').exists()).toBe(false)
    const back = wrapper.findAll('button').find(b => b.text().includes('返回上级'))
    await back.trigger('click'); await flushPromises()
    expect(wrapper.find('.group-card').exists()).toBe(true)
    wrapper.unmount()
  })

  it('pages/filter requests stay on server and clear cursors on filter change', async () => {
    backend()
    const wrapper = mount(View, { props: {category: 'api'} })
    await flushPromises()
    await wrapper.findAll('button').find(b => b.text().includes('全部结果')).trigger('click'); await flushPromises()
    await wrapper.findAll('button').find(b => b.text() === '下一页').trigger('click'); await flushPromises()
    expect(global.fetch.mock.calls.at(-1)[0]).toContain('cursor=next')
    await wrapper.find('[aria-label="搜索当前工作区"]').setValue('/new')
    await wrapper.find('form').trigger('submit'); await flushPromises()
    expect(global.fetch.mock.calls.at(-1)[0]).toContain('q=%2Fnew')
    expect(global.fetch.mock.calls.at(-1)[0]).not.toContain('cursor=')
    wrapper.unmount()
  })

  it('ignores late responses after switching projects, even if fetch ignores abort', async () => {
    let resolveOld
    backend(url => url.includes('project=demo') ? new Promise(resolve => {resolveOld = resolve}) : response({...groups, rows: [{...groups.rows[0], label: 'new.example.com'}]}))
    const wrapper = mount(View, {props: {category: 'assets'}})
    await flushPromises()
    store.project = 'second'; await flushPromises()
    resolveOld(response(groups)); await flushPromises()
    expect(wrapper.text()).toContain('new.example.com')
    expect(wrapper.text()).not.toContain('a.example.com')
    wrapper.unmount()
  })

  it('shows errors and explicit empty states, never substitutes fabricated counts', async () => {
    backend(() => Promise.reject(new Error('offline')))
    const wrapper = mount(View, {props: {category: 'js'}})
    await flushPromises()
    expect(wrapper.find('[role="alert"]').text()).toContain('offline')
    expect(wrapper.text()).toContain('不会执行 JavaScript')
    backend(() => response({...groups, rows: [], total: 0, entity_total: 0}))
    await wrapper.find('[role="alert"] button').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('没有符合条件的结果')
    wrapper.unmount()
  })

  it('resets detail category after cross-navigation before opening another API row', async () => {
    const apiRows = { rows: [
      { id: 20, kind: 'endpoint', entity_key: 'ep2:api-one', display_key: 'GET https://api.example.test/one' },
      { id: 21, kind: 'endpoint', entity_key: 'ep2:api-two', display_key: 'GET https://api.example.test/two' },
    ], total: 2, limit: 50, next_cursor: null }
    global.fetch = vi.fn(async url => {
      const parsed = new URL(String(url), 'http://local')
      if (parsed.pathname.endsWith('/workspace/browse')) {
        return response(parsed.searchParams.get('mode') === 'list' ? apiRows : groups)
      }
      if (parsed.pathname.endsWith('/workspace/entities/20')) return response({
        entity: { id: 20, kind: 'endpoint', display_key: 'GET https://api.example.test/one' },
        rows: [], total: 0, next_cursor: null,
      })
      return response({
        entity: { id: 21, kind: 'endpoint', display_key: 'GET https://api.example.test/two' },
        rows: [], total: 0, next_cursor: null,
      })
    })
    const wrapper = mount(View, { props: { category: 'api' } })
    await flushPromises()
    await wrapper.find('.group-card').trigger('click'); await flushPromises()
    await wrapper.find('.group-card').trigger('click'); await flushPromises()
    await wrapper.findAll('.row-link')[0].trigger('click'); await flushPromises()
    const Detail = (await import('../src/components/WorkspaceDetail.vue')).default
    wrapper.findComponent(Detail).vm.$emit('select', { id: 20, category: 'js', kind: 'endpoint' })
    await flushPromises()
    expect(wrapper.text()).toContain('JS 内容与候选')
    await wrapper.findAll('.row-link')[1].trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('请求样本')
    expect(wrapper.text()).not.toContain('JS 内容与候选')
    wrapper.unmount()
  })
})
