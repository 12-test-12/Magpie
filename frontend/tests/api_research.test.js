import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'

const response = data => ({ ok:true, status:200, json:async () => ({ok:true,data}) })
let store, View, Detail
const groups = {rows:[{group_key:'@api.example.test',label:'api.example.test',count:1}],total:1,entity_total:1,limit:50,next_cursor:null}
const apiRows = {rows:[{id:42,kind:'endpoint',entity_key:'api.example.test/api/research',display_key:'POST https://api.example.test/api/research'}],total:1,limit:50,next_cursor:null}
beforeEach(async () => {
  vi.resetModules()
  window.history.replaceState({}, '', '/?token=test')
  store = (await import('../src/store.js')).store
  store.project = 'demo'
  View = (await import('../src/views/WorkspaceView.vue')).default
  Detail = (await import('../src/components/WorkspaceDetail.vue')).default
})

it('applies API conditions to grouped drilldown, restores navigation and clears other categories', async () => {
  global.fetch = vi.fn(async url => response(new URL(String(url),'http://local').searchParams.get('mode') === 'list' ? apiRows : groups))
  const wrapper = mount(View, {props:{category:'api'}})
  await flushPromises()
  await wrapper.find('[aria-label="参数名筛选"]').setValue('redirect')
  await wrapper.find('[aria-label="参数位置筛选"]').setValue('query')
  await wrapper.find('[aria-label="响应状态码筛选"]').setValue('200')
  await wrapper.find('[aria-label="响应类型筛选"]').setValue('application/json')
  await wrapper.find('[aria-label="凭据线索筛选"]').setValue('cookie')
  await wrapper.find('[aria-label="来源性质筛选"]').setValue('http_sample')
  await wrapper.find('form').trigger('submit'); await flushPromises()
  let query = new URL(global.fetch.mock.calls.at(-1)[0], 'http://local').searchParams
  expect(query.get('parameter_name')).toBe('redirect')
  expect(query.get('response_status')).toBe('200')
  expect(query.get('credential')).toBe('cookie')
  await wrapper.find('.group-card').trigger('click'); await flushPromises()
  query = new URL(global.fetch.mock.calls.at(-1)[0], 'http://local').searchParams
  expect(query.get('parameter_location')).toBe('query')
  expect(query.get('host')).toBe('api.example.test')
  await wrapper.setProps({category:'assets'}); await flushPromises()
  query = new URL(global.fetch.mock.calls.at(-1)[0], 'http://local').searchParams
  expect(query.has('response_status')).toBe(false)
  expect(query.get('parameter_name')).toBe('')
  await wrapper.setProps({category:'api'}); await flushPromises()
  expect(wrapper.find('[aria-label="参数名筛选"]').element.value).toBe('redirect')
  await wrapper.findAll('button').find(b => b.text() === '重置').trigger('click'); await flushPromises()
  query = new URL(global.fetch.mock.calls.at(-1)[0], 'http://local').searchParams
  expect(query.get('parameter_name')).toBe('')
  expect(query.has('response_status')).toBe(false)
  wrapper.unmount()
})

function sample(id, status, credential, extras = {}) {
  return {id, evidence_id:id+100,semantic_field:'http_request_sample',confidence:1,source_count:1,
    value:JSON.stringify({version:1,status,credentials:[credential],query_parameter_names:['redirect'],
      request_header_names:['Cookie'], record_path:`$.log.entries[${id}]`,...extras})}
}

it('retains at most two samples across actual detail pagination and opens each evidence', async () => {
  global.fetch = vi.fn(async url => {
    const parsed = new URL(String(url),'http://local')
    if (parsed.pathname.includes('/evidence/')) return response({id:101,preview:null,preview_reason:'Synthetic evidence',contributions:[]})
    return response({entity:apiRows.rows[0],total:21,next_cursor:parsed.searchParams.has('cursor') ? null : 'second',
      rows:parsed.searchParams.has('cursor') ? [sample(2,401,'bearer',{sample_truncated:true})] : [sample(1,200,'cookie')]})
  })
  const wrapper = mount(Detail, {props:{row:apiRows.rows[0],category:'api'}})
  await flushPromises()
  await wrapper.find('.sample-choices button').trigger('click')
  await wrapper.findAll('.detail-pagination button').find(b => b.text() === '下一页').trigger('click'); await flushPromises()
  expect(wrapper.find('.selected-samples').text()).toContain('样本 #1')
  await wrapper.find('.sample-choices button').trigger('click')
  expect(wrapper.find('.comparison-scroll').text()).toContain('摘要不同')
  expect(wrapper.find('.comparison-scroll').text()).toContain('401')
  expect(wrapper.find('.selected-samples').text()).toContain('摘要已截断')
  expect(wrapper.text()).toContain('不比较参数值')
  await wrapper.findAll('.selected-samples button').find(b => b.text() === '打开样本 #1 证据').trigger('click'); await flushPromises()
  expect(wrapper.text()).toContain('原始证据 #101')
  await wrapper.findAll('button').find(b => b.text().includes('返回来源列表')).trigger('click')
  await wrapper.setProps({row:{...apiRows.rows[0],id:43}}); await flushPromises()
  expect(wrapper.find('.selected-samples').exists()).toBe(false)
  wrapper.unmount()
})

it('drops comparison snapshots when switching projects or effective data changes', async () => {
  const Comparison = (await import('../src/components/RequestSampleComparison.vue')).default
  const wrapper = mount(Comparison, {props:{rows:[sample(1,200,'cookie'),sample(2,401,'bearer'),sample(3,204,'unknown')],context:'a:42:0'}})
  await wrapper.findAll('.sample-choices button')[0].trigger('click')
  await wrapper.findAll('.sample-choices button')[1].trigger('click')
  expect(wrapper.findAll('.sample-choices button')[2].element.disabled).toBe(true)
  await wrapper.setProps({context:'b:42:0'})
  expect(wrapper.find('.selected-samples').exists()).toBe(false)
  wrapper.unmount()
})

it('reloads effective sample rows and clears selected snapshots after data changes', async () => {
  let removed = false
  global.fetch = vi.fn(async () => response({entity:apiRows.rows[0],total:removed ? 0 : 1,next_cursor:null,
    rows:removed ? [] : [sample(1,200,'cookie')]}))
  const wrapper = mount(Detail, {props:{row:apiRows.rows[0],category:'api'}})
  await flushPromises()
  await wrapper.find('.sample-choices button').trigger('click')
  removed = true; store.dataRevision++
  await flushPromises()
  expect(wrapper.find('.selected-samples').exists()).toBe(false)
  expect(wrapper.find('.sample-choices button').exists()).toBe(false)
  expect(wrapper.text()).toContain('当前没有已导入并关联到此接口的 HTTP 请求样本')
  wrapper.unmount()
})
