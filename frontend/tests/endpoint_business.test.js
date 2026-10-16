import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const response = data => ({ ok: true, status: 200, json: async () => ({ ok: true, data }) })
let store, Detail

beforeEach(async () => {
  vi.resetModules()
  window.history.replaceState({}, '', '/?token=test')
  store = (await import('../src/store.js')).store
  store.project = 'demo'
  Detail = (await import('../src/components/WorkspaceDetail.vue')).default
})

describe('Endpoint business details', () => {
  it('starts with one bounded, masked request sample and opens its evidence', async () => {
    const secret = 'SYNTHETIC-SECRET'
    global.fetch = vi.fn(async url => {
      const path = new URL(String(url), 'http://local')
      if (path.searchParams.get('section') === 'samples') return response({
        entity: { id: 42, kind: 'endpoint', display_key: 'GET https://api.example.test/items', identity_status: 'exact_v2' },
        total: 1, next_cursor: null,
        rows: [{ id: 8, semantic_field: 'http_request_sample', confidence: 1, source_count: 1,
          source: 'capture.har', evidence_id: 77,
          value: JSON.stringify({ url: 'https://api.example.test/items?password=%5Bredacted%5D',
            url_truncated: false, method: 'GET', status: 200, query_parameter_names: ['password'],
            body_parameter_names: ['name'], body_parameters_state: 'complete',
            request_header_names: ['Authorization'], request_headers_state: 'complete',
            credentials: ['bearer', 'unknown'], response_mime: 'application/json', response_size: 12,
            record_path: '$.log.entries[0]', sample_truncated: false }) }],
      })
      return response({ id: 77, locator_uri: 'archive', locator: '$.log.entries[0]',
        preview: null, preview_reason: 'synthetic', contributions: [], contributions_truncated: false })
    })

    const wrapper = mount(Detail, { props: {
      row: { id: 42, kind: 'endpoint', display_key: 'GET https://api.example.test/items' }, category: 'api',
    } })
    await flushPromises()
    expect(wrapper.text()).toContain('请求样本')
    expect(wrapper.text()).toContain('password')
    expect(wrapper.text()).toContain('观察到 Bearer')
    expect(wrapper.text()).toContain('凭据状态未知')
    expect(wrapper.text()).not.toContain(secret)
    await wrapper.findAll('button').find(button => button.text().includes('打开此样本的来源证据')).trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('原始证据 #77')
    expect(wrapper.emitted('select')).toBeUndefined()
    wrapper.unmount()
  })

  it('labels legacy identity and hides non-structural parameter values', async () => {
    global.fetch = vi.fn(async () => response({
      entity: { id: 43, kind: 'endpoint', display_key: 'legacy.host/path/{id}',
        identity_status: 'legacy_folded', identity_notice: '历史折叠身份，无法完整恢复' },
      total: 2, next_cursor: null,
      rows: [
        { id: 1, semantic_field: 'param.query.password', value: 'query', confidence: 1, source_count: 1 },
        { id: 2, semantic_field: 'param.body.example.description', value: '[未在参数结构页展示]', confidence: 1, source_count: 1 },
      ],
    }))
    const wrapper = mount(Detail, { props: {
      row: { id: 43, kind: 'endpoint', display_key: 'legacy.host/path/{id}', identity_status: 'legacy_folded' }, category: 'api',
    } })
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text().includes('参数结构')).trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('历史折叠身份')
    expect(wrapper.text()).toContain('password')
    expect(wrapper.text()).toContain('query')
    expect(wrapper.text()).toContain('不展示参数值或说明文本')
    wrapper.unmount()
  })
})
