import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const response = data => ({ ok: true, status: 200, json: async () => ({ ok: true, data }) })
let store, Detail
const hash = 'a'.repeat(64)

beforeEach(async () => {
  vi.resetModules()
  window.history.replaceState({}, '', '/?token=test')
  store = (await import('../src/store.js')).store
  store.project = 'demo'
  Detail = (await import('../src/components/WorkspaceDetail.vue')).default
})

describe('JS candidate provenance navigation', () => {
  it('shows version and candidate, opens API, then returns via source reference', async () => {
    global.fetch = vi.fn(async url => {
      const path = new URL(String(url), 'http://local')
      if (path.pathname.endsWith('/workspace/entities/10')) return response({
        entity: { id: 10, entity_key: 'app.example.test/app.js' }, total: 2, next_cursor: null,
        rows: [
          { id: 1, semantic_field: 'js_content_version', value: JSON.stringify({ sha256: hash, status: 'bounded_rules_complete', candidate_count: 1, unsupported_count: 0, rule_id: 'js-literal-call', rule_version: '1', record_path: '$.log.entries[0].response.content.text' }) },
          { id: 2, semantic_field: 'js_candidate', value: JSON.stringify({ url: 'https://api.example.test/v1', method: 'GET', start: 8, end: 40, line: 1, snippet: "fetch('https://api.example.test/v1')", rule_id: 'js-literal-call', rule_version: '1', content_sha256: hash, record_path: '$.log.entries[0].response.content.text', target_id: 20 }) },
        ],
      })
      return response({
        entity: { id: 20, entity_key: 'api.example.test/v1' }, total: 1, next_cursor: null,
        rows: [{ id: 3, semantic_field: 'js_source_ref', confidence: 0.8, source_count: 1, value: JSON.stringify({ resource_key: 'app.example.test/app.js', resource_id: 10, content_sha256: hash, record_path: '$.log.entries[0].response.content.text' }) }],
      })
    })

    const wrapper = mount(Detail, { props: { row: { id: 10, kind: 'endpoint', entity_key: 'app.example.test/app.js' }, category: 'js' } })
    await flushPromises()
    expect(wrapper.text()).toContain('内容版本')
    expect(wrapper.text()).toContain(hash)
    expect(wrapper.text()).toContain('静态代码引用候选')
    await wrapper.findAll('button').find(button => button.text().includes('打开关联 API')).trigger('click')
    expect(wrapper.emitted('select').at(-1)[0]).toEqual({ id: 20, category: 'api', kind: 'entity' })

    await wrapper.setProps({ row: { id: 20, kind: 'endpoint', entity_key: 'api.example.test/v1' }, category: 'api' })
    await flushPromises()
    expect(wrapper.text()).toContain('JS 来源引用')
    expect(wrapper.text()).toContain(hash)
    await wrapper.findAll('button').find(button => button.text().includes('打开 JS 来源版本')).trigger('click')
    expect(wrapper.emitted('select').at(-1)[0]).toEqual({ id: 10, category: 'js', kind: 'endpoint' })
    wrapper.unmount()
  })
})
