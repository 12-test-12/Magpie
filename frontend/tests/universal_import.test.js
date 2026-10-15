import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import UniversalImport from '../src/components/UniversalImport.vue'
import ImportCoverage from '../src/components/ImportCoverage.vue'

vi.mock('../src/store.js', () => ({ store: { project: 'demo' } }))
const post = vi.fn()
const get = vi.fn(async () => [])
vi.mock('../src/api.js', () => ({ post: (...args) => post(...args), get: (...args) => get(...args) }))

const data = (mapping = {}) => ({ format: 'csv', complete: true, error: null,
  records_total: 1, unrecognized: 1, sha256: 'abc', structure_signature: '0123456789abcdef',
  mapping, fields: { '$.destination': { sample: 'https://a.example.com', suggested: 'auto' } }, details: [] })

afterEach(() => vi.clearAllMocks())
const button = (wrapper, text) => wrapper.findAll('button').find(b => b.text().includes(text))

describe('通用导入确认与来源变化', () => {
  it('修改映射后必须重新预览，切换文件会取消确认', async () => {
    post.mockImplementation(async (_url, body) => data(body.mapping))
    const wrapper = mount(UniversalImport, { props: { path: 'custom.csv' } })
    await button(wrapper, '预览格式').trigger('click'); await flushPromises()
    await wrapper.get('select[aria-label="映射 $.destination"]').setValue('url')
    await button(wrapper, '确认此映射').trigger('click')
    expect(wrapper.text()).toContain('请重新预览后确认')
    expect(wrapper.emitted('ready').at(-1)).toEqual([null])
    await button(wrapper, '预览格式').trigger('click'); await flushPromises()
    await button(wrapper, '确认此映射').trigger('click')
    expect(wrapper.emitted('ready').at(-1)[0]).toMatchObject({ path: 'custom.csv', mapping: { '$.destination': 'url' } })
    await wrapper.setProps({ path: 'other.csv' })
    expect(wrapper.emitted('ready').at(-1)).toEqual([null])
    expect(wrapper.findComponent(ImportCoverage).exists()).toBe(false)
  })

  it('文件切换后丢弃迟到的预览响应', async () => {
    let resolve
    post.mockImplementation(() => new Promise(r => { resolve = r }))
    const wrapper = mount(UniversalImport, { props: { path: 'a.csv' } })
    await button(wrapper, '预览格式').trigger('click')
    await wrapper.setProps({ path: 'b.csv' })
    resolve(data()); await flushPromises()
    expect(wrapper.findComponent(ImportCoverage).exists()).toBe(false)
    expect(wrapper.emitted('ready').at(-1)).toEqual([null])
  })

  it('显示不完整和截断提示，不能当成完整覆盖', () => {
    const wrapper = mount(ImportCoverage, { props: { coverage: { ...data(), complete: false, details_truncated: true } } })
    expect(wrapper.text()).toContain('处理未完成')
    expect(wrapper.text()).toContain('仅显示前 200 条记录')
  })
})
