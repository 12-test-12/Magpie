import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'

const response = data => ({ ok: true, status: 200, json: async () => ({ ok: true, data }) })
const finding = (id, title) => ({ id, kind: 'vuln', title, triage: { status: 'candidate', severity: 'high', version: 1 } })
let store, Panel
beforeEach(async () => {
  vi.resetModules()
  window.history.replaceState({}, '', '/?token=test')
  store = (await import('../src/store.js')).store
  store.project = 'first'
  Panel = (await import('../src/components/FindingPanel.vue')).default
})

it('shows a finding load failure and retries without exposing editable defaults', async () => {
  global.fetch = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValue(response(finding(1, '复核结果')))
  const wrapper = mount(Panel, { props: { findingId: 1, section: 'triage' } })
  await flushPromises()
  expect(wrapper.find('[role="alert"]').text()).toContain('offline')
  expect(wrapper.find('form').exists()).toBe(false)
  await wrapper.find('[role="alert"] button').trigger('click'); await flushPromises()
  expect(wrapper.text()).toContain('复核结果')
  expect(wrapper.find('form').exists()).toBe(true)
  wrapper.unmount()
})

it('ignores a late finding response after project/target changes', async () => {
  let old
  global.fetch = vi.fn(url => String(url).includes('project=first')
    ? new Promise(resolve => { old = resolve }) : Promise.resolve(response(finding(2, '新项目发现'))))
  const wrapper = mount(Panel, { props: { findingId: 1, section: 'triage' } })
  await flushPromises()
  store.project = 'second'; await wrapper.setProps({ findingId: 2 }); await flushPromises()
  old(response(finding(1, '旧项目发现'))); await flushPromises()
  expect(wrapper.text()).toContain('新项目发现')
  expect(wrapper.text()).not.toContain('旧项目发现')
  wrapper.unmount()
})

it('pages retests through the bounded endpoint and opens evidence', async () => {
  global.fetch = vi.fn(async url => {
    const parsed = new URL(String(url), 'http://local')
    if (parsed.pathname.includes('/workspace/findings/')) return response(finding(1, '分页发现'))
    const second = parsed.searchParams.has('cursor')
    return response({ total: 21, rows: [{ id: second ? 21 : 1, status: 'inconclusive', notes: second ? '第二页复测' : '第一页复测', evidence_id: 8 }], next_cursor: second ? null : 'page-two' })
  })
  const wrapper = mount(Panel, { props: { findingId: 1, section: 'retests' } })
  await flushPromises()
  expect(wrapper.text()).toContain('第一页复测')
  await wrapper.findAll('button').find(b => b.text() === '下一页').trigger('click'); await flushPromises()
  expect(wrapper.text()).toContain('第二页复测')
  expect(global.fetch.mock.calls.at(-1)[0]).toContain('cursor=page-two')
  await wrapper.findAll('button').find(b => b.text().includes('查看复测关联证据')).trigger('click')
  expect(wrapper.emitted('evidence')[0]).toEqual([8])
  wrapper.unmount()
})

it('does not apply an old mutation completion to another project', async () => {
  let finish
  global.fetch = vi.fn(async (url, options) => {
    if (options?.method === 'POST') return new Promise(resolve => { finish = resolve })
    return response(finding(1, store.project === 'first' ? '旧发现' : '新发现'))
  })
  const wrapper = mount(Panel, { props: { findingId: 1, section: 'triage' } })
  await flushPromises()
  await wrapper.find('form').trigger('submit'); await flushPromises()
  store.project = 'second'; await flushPromises()
  const revision = store.dataRevision
  finish(response({ finding_id: 1 })); await flushPromises()
  expect(wrapper.text()).toContain('新发现')
  expect(wrapper.text()).not.toContain('已更新并计入审计')
  expect(store.dataRevision).toBe(revision)
  wrapper.unmount()
})

it('preserves a conflicted decision draft and requires explicit reload before another write', async () => {
  let revision = 1
  global.fetch = vi.fn(async (url, options) => {
    if (options?.method === 'POST') return {ok:false, status:409, json:async () => ({ok:false, error:{code:'E_STALE_FINDING', message:'发现已被修改'}})}
    return response({...finding(1, '冲突发现'), triage:{status:'candidate', severity:'high', version:revision}})
  })
  const wrapper = mount(Panel, {props:{findingId:1, section:'triage'}})
  await flushPromises()
  await wrapper.find('textarea').setValue('旧窗口草稿，需要保留')
  await wrapper.find('form').trigger('submit'); await flushPromises()
  const posts = () => global.fetch.mock.calls.filter(([,o]) => o?.method === 'POST')
  expect(JSON.parse(posts()[0][1].body).expected_version).toBe(1)
  expect(wrapper.find('textarea').element.value).toBe('旧窗口草稿，需要保留')
  expect(wrapper.find('[role="alert"]').text()).toContain('本次未写入')
  await wrapper.find('form').trigger('submit'); await flushPromises()
  expect(posts()).toHaveLength(1)
  revision = 2
  await wrapper.find('[role="alert"] button').trigger('click'); await flushPromises()
  expect(wrapper.find('textarea').element.value).toBe('')
  await wrapper.find('form').trigger('submit'); await flushPromises()
  expect(JSON.parse(posts().at(-1)[1].body).expected_version).toBe(2)
  wrapper.unmount()
})

it('blocks an unsupported decision and sends the loaded revision with a retest', async () => {
  global.fetch = vi.fn(async (url, options) => options?.method === 'POST'
    ? response({version:2}) : String(url).includes('/workspace/entities/')
      ? response({rows:[],total:0,next_cursor:null}) : response(finding(1, '人工依据')))
  const wrapper = mount(Panel, {props:{findingId:1, section:'triage'}})
  await flushPromises()
  await wrapper.find('select').setValue('confirmed')
  await wrapper.find('form').trigger('submit'); await flushPromises()
  expect(wrapper.text()).toContain('必须填写理由')
  expect(global.fetch.mock.calls.some(([,o]) => o?.method === 'POST')).toBe(false)
  await wrapper.setProps({section:'retests'}); await flushPromises()
  expect(wrapper.text()).toContain('未关联复测证据，仅有人工说明')
  await wrapper.find('textarea').setValue('本地人工复测说明')
  await wrapper.find('form').trigger('submit'); await flushPromises()
  const [, options] = global.fetch.mock.calls.find(([,o]) => o?.method === 'POST')
  expect(JSON.parse(options.body)).toMatchObject({expected_version:1, status:'inconclusive'})
  expect(wrapper.text()).toContain('原处置判定保留')
  wrapper.unmount()
})
