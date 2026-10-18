import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const response = data => ({ok:true, status:200, json:async () => ({ok:true, data})})
let store, Palette, Tasks, Metadata
const host = {id:12, kind:'host', entity_key:'portal.example.test', display_key:'portal.example.test', available:true}
const task = {id:7, title:'申请测试账号', description:'等待客户确认', status:'pending', version:1, links:[host], updated_at:'2026-10-17'}
const groups = [{category:'assets', rows:[host], total:23, next_cursor:'more'},
  {category:'notes', rows:[{id:70,kind:'note',entity_id:12,text:'portal 研究线索',target_display:'portal.example.test'}],total:1}]

beforeEach(async () => {
  vi.resetModules(); vi.useFakeTimers()
  window.history.replaceState({}, '', '/?token=test')
  store = (await import('../src/store.js')).store; store.project = 'demo'
  Palette = (await import('../src/components/CommandPalette.vue')).default
  Tasks = (await import('../src/views/TasksView.vue')).default
  Metadata = (await import('../src/components/EntityResearch.vue')).default
})
afterEach(() => { vi.useRealTimers() })

it('opens the same Finding and open-task scope as the overview counts', async () => {
  const Overview = (await import('../src/views/OverviewView.vue')).default
  store.projects = [{id:1,name:'demo',counts:{entities:10}}]
  global.fetch = vi.fn(async () => response({assets:2, api:2, evidence:1, findings_confirmed:1, findings_candidate:2,
    tasks_open:2, recent_jobs:[], count_basis:'合成数据'}))
  const wrapper = mount(Overview); await flushPromises()
  await wrapper.findAll('button').find(b => b.text().includes('已确认漏洞')).trigger('click')
  expect(store.workspaceJump.filters).toEqual({status:'confirmed'})
  await wrapper.findAll('button').find(b => b.text().includes('工具发现')).trigger('click')
  expect(store.workspaceJump.filters).toEqual({status:'candidate'})
  await wrapper.findAll('button').find(b => b.text().includes('待处理与进行中')).trigger('click')
  expect(store.workspaceJump.filters).toEqual({status:'open'})
  wrapper.unmount()
  global.fetch = vi.fn(async () => response({rows:[],total:2,next_cursor:null}))
  const tasks = mount(Tasks); await flushPromises()
  expect(tasks.find('[aria-label="筛选任务状态"]').element.value).toBe('open')
  expect(global.fetch.mock.calls.at(-1)[0]).toContain('status=open')
  tasks.unmount()
})

it('searches the backend, groups counts, and opens a result directly without a graph', async () => {
  global.fetch = vi.fn(async () => response({groups}))
  const wrapper = mount(Palette)
  store.paletteOpen = true; await flushPromises()
  await wrapper.find('input').setValue('portal'); await vi.advanceTimersByTimeAsync(260); await flushPromises()
  expect(global.fetch.mock.calls[0][0]).toContain('/workspace/search?project=demo&q=portal')
  expect(wrapper.text()).toContain('资产 · 23')
  await wrapper.find('input').trigger('keydown', {key:'ArrowDown'})
  await wrapper.find('input').trigger('keydown', {key:'Enter'})
  expect(store.view).toBe('notes')
  expect(store.workspaceJump.row.id).toBe(70)
  expect(store.paletteOpen).toBe(false)
  expect(global.fetch.mock.calls.some(([url]) => url.includes('/graph'))).toBe(false)
  wrapper.unmount()
})

it('cancels obsolete searches on keyword/project changes and can retry real failures', async () => {
  let old
  global.fetch = vi.fn(() => new Promise(resolve => {old = resolve}))
  const wrapper = mount(Palette); store.paletteOpen = true; await flushPromises()
  await wrapper.find('input').setValue('old'); await vi.advanceTimersByTimeAsync(260)
  store.project = 'other'; await flushPromises(); old(response({groups})); await flushPromises()
  expect(wrapper.find('.search-hit').exists()).toBe(false)
  global.fetch = vi.fn(async () => {throw new Error('offline')})
  await wrapper.find('input').setValue('new'); await vi.advanceTimersByTimeAsync(260); await flushPromises()
  expect(wrapper.find('[role="alert"]').text()).toContain('offline')
  global.fetch = vi.fn(async () => response({groups:[]}))
  await wrapper.find('[role="alert"] button').trigger('click'); await flushPromises()
  expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  wrapper.unmount()
})

it('creates a linked task and saves all four statuses with the current version', async () => {
  global.fetch = vi.fn(async (url, options = {}) => {
    if (options.method === 'POST' || options.method === 'PUT') return response({...task, ...JSON.parse(options.body), links:[host], version:2})
    return response({rows:[task], total:1, next_cursor:null})
  })
  store.openWorkspace('tasks', {target:host})
  const wrapper = mount(Tasks); await flushPromises()
  expect(wrapper.find('[aria-label="研究任务编辑"]').text()).toContain('portal.example.test')
  await wrapper.find('[aria-label="任务标题"]').setValue('验证控制台')
  for (const status of ['pending','in_progress','done','shelved']) {
    await wrapper.find('[aria-label="任务状态"]').setValue(status)
    await wrapper.find('.task-editor form').trigger('submit'); await flushPromises()
    const [, options] = global.fetch.mock.calls.filter(([,o]) => ['POST','PUT'].includes(o.method)).at(-1)
    const body = JSON.parse(options.body)
    expect(body.status).toBe(status); expect(body.entity_ids).toEqual([12])
    expect(wrapper.text()).toContain('研究任务已保存')
  }
  wrapper.unmount()
})

it('pages tasks on the server and clears state and late writes on project change', async () => {
  let write
  global.fetch = vi.fn(async (url, options = {}) => {
    if (options.method === 'PUT') return new Promise(resolve => {write=resolve})
    return response({rows:[task], total:51, next_cursor:'page-two'})
  })
  const wrapper = mount(Tasks); await flushPromises()
  await wrapper.findAll('button').find(b => b.text() === '下一页').trigger('click'); await flushPromises()
  expect(global.fetch.mock.calls.at(-1)[0]).toContain('cursor=page-two')
  await wrapper.find('.task-card .row-link').trigger('click')
  await wrapper.find('.task-editor form').trigger('submit'); await flushPromises()
  store.project = 'other'; await flushPromises(); write(response({...task, version:2})); await flushPromises()
  expect(wrapper.find('.task-editor').exists()).toBe(false)
  expect(wrapper.text()).not.toContain('研究任务已保存')
  expect(global.fetch.mock.calls.at(-1)[0]).not.toContain('cursor=')
  wrapper.unmount()
})

it('saves tags as deltas and criticality separately, then reports a stale version', async () => {
  const meta = {version:3, criticality:2, tags:['old','payment']}
  global.fetch = vi.fn(async (url, options={}) => options.method === 'PUT'
    ? {ok:false, status:409, json:async () => ({ok:false,error:{code:'E_STALE_RESEARCH',message:'请重新载入'}})} : response(meta))
  const wrapper = mount(Metadata, {props:{entityId:12}}); await flushPromises()
  await wrapper.find('[aria-label="人工标签"]').setValue('payment, core-system')
  await wrapper.find('[aria-label="资产重要度"]').setValue('1')
  await wrapper.find('form').trigger('submit'); await flushPromises()
  expect(JSON.parse(global.fetch.mock.calls.at(-1)[1].body)).toEqual({version:3, criticality:1,add_tags:['core-system'],remove_tags:['old']})
  expect(wrapper.find('[role="alert"]').text()).toContain('重新载入')
  wrapper.unmount()
})

it('workspace consumes a search jump to a note and displays its matched text', async () => {
  const Workspace = (await import('../src/views/WorkspaceView.vue')).default
  const note = {id:70,kind:'note',entity_id:12,target_kind:'host',text:'portal 研究线索',target_display:'portal.example.test'}
  global.fetch = vi.fn(async url => response(url.includes('/workspace/browse')
    ? {rows:[note], total:1, next_cursor:null, limit:50} : {entity:host,rows:[],total:0}))
  store.openWorkspace('notes', note, {q:'portal'})
  const wrapper = mount(Workspace, {props:{category:'notes'}}); await flushPromises()
  expect(wrapper.find('.workspace-detail').text()).toContain('所选笔记')
  expect(wrapper.find('.workspace-detail').text()).toContain('portal 研究线索')
  expect(wrapper.find('[aria-label="搜索当前工作区"]').element.value).toBe('portal')
  expect(store.workspaceJump).toBe(null)
  wrapper.unmount()
})

it('keeps a direct search selection when an existing workspace changes category', async () => {
  const Workspace = (await import('../src/views/WorkspaceView.vue')).default
  const note = {id:70,kind:'note',entity_id:12,target_kind:'host',text:'portal 研究线索',target_display:'portal.example.test'}
  global.fetch = vi.fn(async url => response(url.includes('/workspace/browse')
    ? {rows:[], total:0, next_cursor:null, limit:50} : {entity:host,rows:[],total:0}))
  const wrapper = mount(Workspace, {props:{category:'evidence'}}); await flushPromises()
  store.openWorkspace('notes', note, {q:'portal'})
  await wrapper.setProps({category:'notes'}); await flushPromises()
  expect(wrapper.find('[aria-label="搜索当前工作区"]').element.value).toBe('portal')
  expect(wrapper.find('.workspace-detail').text()).toContain('portal 研究线索')
  wrapper.unmount()
})
