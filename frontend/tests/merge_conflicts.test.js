import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import { mergeFields } from '../src/merge.js'

const response = (data, code='') => ({ok:!code,status:code ? 409 : 200,json:async () => code ? {ok:false,error:{code,message:'版本冲突'}} : {ok:true,data}})
let store
beforeEach(async () => {
  vi.resetModules(); window.history.replaceState({},'', '/?token=test')
  store = (await import('../src/store.js')).store; store.project = 'demo'
})
const button = (wrapper, text) => wrapper.findAll('button').find(b => b.text() === text)

it.each([
  ['base','new','base','latest'], ['base','base','mine','draft'],
  ['base','same','same','latest'], ['base','new','mine',''],
  [false,true,false,'latest'], [[1],[1,2],[1],'latest'], [[1],[1,2],[1,3],''],
])('selects only unambiguous defaults: %j / %j / %j', (original,latest,draft,choice) => {
  expect(mergeFields([{key:'value',label:'字段'}], {value:original}, {value:latest}, {value:draft})[0].choice).toBe(choice)
})

it('keeps newer manual review state while applying an explicitly selected name', async () => {
  const Manual = (await import('../src/components/ManualValues.vue')).default
  let version = 0, payload, writes = 0
  global.fetch = vi.fn(async (url,options={}) => {
    if (options.method === 'PATCH') {
      writes++; payload = JSON.parse(options.body)
      return writes === 1 ? response(null,'E_STALE_RESEARCH') : response({manual_values:{display_name:payload.display_name,review_status:payload.review_status,version:2}})
    }
    return response({display_name:version ? '另一窗口名称' : null,review_status:version ? 'reviewed' : null,version})
  })
  const wrapper = mount(Manual,{props:{entityId:12}}); await flushPromises()
  await wrapper.get('[aria-label="人工名称"]').setValue('我的名称')
  await wrapper.get('[aria-label="人工字段修改理由"]').setValue('研究标识')
  await wrapper.get('form').trigger('submit'); await flushPromises(); version = 1
  await button(wrapper,'核对最新值与草稿').trigger('click'); await flushPromises()
  expect(button(wrapper,'应用选择到草稿').attributes('disabled')).toBeDefined()
  await wrapper.get('[aria-label="字段选择：人工名称"]').setValue('draft')
  await button(wrapper,'应用选择到草稿').trigger('click'); await flushPromises()
  expect(writes).toBe(1)
  expect(wrapper.get('[aria-label="人工审核状态"]').element.value).toBe('reviewed')
  await wrapper.get('form').trigger('submit'); await flushPromises()
  expect(payload).toMatchObject({display_name:'我的名称',review_status:'reviewed',expected_version:1,reason:'研究标识'})
  wrapper.unmount()
})

it('requires explicit evidence choice, keeps draft on cancel/failure and rejects another stale write', async () => {
  const Editor = (await import('../src/components/NoteEditor.vue')).default
  const base = {id:70,entity_id:12,target_kind:'host',text:'原文',version:0,archived:false,available:true,evidence_links:[{id:1,kind:'evidence',display_key:'原证据'}]}
  let latest = base, fail = false, payload
  global.fetch = vi.fn(async (url,options={}) => {
    if (options.method === 'PUT') {payload = JSON.parse(options.body); return response(null,'E_STALE_RESEARCH')}
    if (fail) throw Error('读取失败')
    return response(latest)
  })
  const wrapper = mount(Editor,{props:{noteId:70}}); await flushPromises()
  await wrapper.get('[aria-label="编辑笔记正文"]').setValue('我的草稿')
  await wrapper.get('[aria-label="笔记修改理由"]').setValue('依据')
  await wrapper.get('[aria-label="移除关联 1"]').trigger('click')
  await wrapper.get('form').trigger('submit'); await flushPromises()
  fail = true
  await button(wrapper,'核对最新值与草稿').trigger('click'); await flushPromises()
  expect(wrapper.get('[aria-label="编辑笔记正文"]').element.value).toBe('我的草稿')
  expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeDefined()
  fail = false; latest = {...base,version:2,evidence_links:[...base.evidence_links,{id:2,kind:'evidence',display_key:'新证据'}]}
  await button(wrapper,'核对最新值与草稿').trigger('click'); await flushPromises()
  expect(wrapper.get('[aria-label="字段选择：关联研究证据"]').element.value).toBe('')
  await button(wrapper,'取消核对，保留草稿').trigger('click'); await flushPromises()
  expect(wrapper.get('[aria-label="编辑笔记正文"]').element.value).toBe('我的草稿')
  await button(wrapper,'核对最新值与草稿').trigger('click'); await flushPromises()
  await wrapper.get('[aria-label="字段选择：关联研究证据"]').setValue('latest')
  await button(wrapper,'应用选择到草稿').trigger('click'); await flushPromises()
  latest = {...latest,version:3,archived:true}
  await wrapper.get('form').trigger('submit'); await flushPromises()
  expect(payload).toMatchObject({expected_version:2,evidence_ids:[1,2]})
  expect(wrapper.get('button[type="submit"]').attributes('disabled')).toBeDefined()
  await button(wrapper,'核对最新值与草稿').trigger('click'); await flushPromises()
  expect(wrapper.get('[aria-label="字段选择：归档状态"]').element.value).toBe('latest')
  wrapper.unmount()
})

it('ignores latest-note reads that finish after a project switch', async () => {
  const Editor = (await import('../src/components/NoteEditor.vue')).default
  const note = {id:70,entity_id:12,text:'原文',version:0,archived:false,available:true,evidence_links:[]}
  let finish, hold = false
  global.fetch = vi.fn(async (url,options={}) => options.method === 'PUT' ? response(null,'E_STALE_RESEARCH') :
    hold && url.includes('project=demo') ? new Promise(resolve => {finish=resolve}) : response(note))
  const wrapper = mount(Editor,{props:{noteId:70}}); await flushPromises()
  await wrapper.get('[aria-label="笔记修改理由"]').setValue('依据')
  await wrapper.get('form').trigger('submit'); await flushPromises(); hold = true
  await button(wrapper,'核对最新值与草稿').trigger('click'); await flushPromises()
  store.project = 'other'; await flushPromises()
  finish(response({...note,version:8,text:'旧项目'})); await flushPromises()
  expect(wrapper.find('.conflict-resolver').exists()).toBe(false)
  expect(wrapper.get('[aria-label="编辑笔记正文"]').element.value).toBe('原文')
  wrapper.unmount()
})
