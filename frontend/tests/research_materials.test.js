import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'

const response = (data, status=200, code='') => ({ok:status < 400,status,json:async () => status < 400 ? {ok:true,data} : {ok:false,error:{code,message:'版本冲突'}}})
let store, Editor
const note = {id:70,kind:'note',entity_id:12,target_kind:'host',entity_key:'portal.example.test',text:'原始线索',original_text:'原始线索',version:0,archived:false,available:true,evidence_id:9,evidence_links:[]}
beforeEach(async () => {
  vi.resetModules(); window.history.replaceState({},'', '/?token=test')
  store = (await import('../src/store.js')).store; store.project = 'demo'
  Editor = (await import('../src/components/NoteEditor.vue')).default
})

it('preserves note drafts after stale writes and requires explicit reload', async () => {
  let version = 0
  global.fetch = vi.fn(async (url,opts={}) => {
    if (opts.method === 'PUT') return response(null,409,'E_STALE_RESEARCH')
    return response({...note,version,text:version ? '另一窗口内容' : note.text})
  })
  const wrapper = mount(Editor,{props:{noteId:70}}); await flushPromises()
  await wrapper.find('[aria-label="编辑笔记正文"]').setValue('我的草稿')
  await wrapper.find('[aria-label="笔记修改理由"]').setValue('补充')
  await wrapper.find('form').trigger('submit'); await flushPromises()
  expect(wrapper.find('[aria-label="编辑笔记正文"]').element.value).toBe('我的草稿')
  expect(wrapper.find('button[type="submit"]').attributes('disabled')).toBeDefined()
  expect(JSON.parse(global.fetch.mock.calls.find(([,o]) => o.method === 'PUT')[1].body).expected_version).toBe(0)
  version = 1
  await wrapper.findAll('button').find(b => b.text() === '重新载入笔记').trigger('click'); await flushPromises()
  expect(wrapper.find('[aria-label="编辑笔记正文"]').element.value).toBe('另一窗口内容')
  wrapper.unmount()
})

it('allows adopting latest version while preserving user draft', async () => {
  let version = 0
  global.fetch = vi.fn(async (url,opts={}) => {
    if (opts.method === 'PUT') return response(null,409,'E_STALE_RESEARCH')
    return response({...note,version,text:version ? '另一窗口内容' : note.text})
  })
  const wrapper = mount(Editor,{props:{noteId:70}}); await flushPromises()
  await wrapper.find('[aria-label="编辑笔记正文"]').setValue('我的重要草稿')
  await wrapper.find('[aria-label="笔记修改理由"]').setValue('补充理由')
  await wrapper.find('form').trigger('submit'); await flushPromises()
  expect(wrapper.find('button[type="submit"]').attributes('disabled')).toBeDefined()
  version = 2
  await wrapper.findAll('button').find(b => b.text() === '保留草稿并同步最新版本').trigger('click'); await flushPromises()
  expect(wrapper.find('[aria-label="编辑笔记正文"]').element.value).toBe('我的重要草稿')
  expect(wrapper.find('button[type="submit"]').attributes('disabled')).toBeUndefined()
  wrapper.unmount()
})

it('records archive and evidence references without replacing the original text', async () => {
  let payload
  global.fetch = vi.fn(async (url,opts={}) => {
    if (opts.method === 'PUT') {payload = JSON.parse(opts.body); return response({...note,...payload,version:1})}
    return response(url.includes('/history?') ? {rows:[],total:0} : {...note,evidence_links:[{id:10,kind:'evidence',display_key:'HAR'}]})
  })
  const wrapper = mount(Editor,{props:{noteId:70}}); await flushPromises()
  await wrapper.find('[aria-label="归档笔记"]').setValue(true)
  await wrapper.find('[aria-label="笔记修改理由"]').setValue('已整理')
  await wrapper.find('form').trigger('submit'); await flushPromises()
  expect(payload).toMatchObject({archived:true,evidence_ids:[10],expected_version:0})
  expect(wrapper.text()).toContain('原始线索')
  wrapper.unmount()
})

it('ignores note writes completing after switching project', async () => {
  let finish
  global.fetch = vi.fn(async (url,opts={}) => opts.method === 'PUT' ? new Promise(resolve => {finish=resolve}) : response(note))
  const wrapper = mount(Editor,{props:{noteId:70}}); await flushPromises()
  await wrapper.find('[aria-label="笔记修改理由"]').setValue('补充')
  await wrapper.find('form').trigger('submit'); await flushPromises()
  store.project = 'other'; await flushPromises()
  const revision = store.dataRevision
  finish(response({...note,version:1})); await flushPromises()
  expect(store.dataRevision).toBe(revision)
  expect(wrapper.text()).not.toContain('笔记修订已保存')
  wrapper.unmount()
})

it('stages explicit JS text with its resource URL and native parser', async () => {
  const Local = (await import('../src/components/LocalTextInput.vue')).default
  global.fetch = vi.fn(async () => response({path:'D:/synthetic/local-js.manifest.json',sha256:'hash'}))
  const wrapper = mount(Local)
  await wrapper.find('[aria-label="本地输入类型"]').setValue('js')
  await wrapper.find('[aria-label="JS 资源绝对 URL"]').setValue('https://portal.example.test/app.js')
  await wrapper.find('[aria-label="粘贴文件名"]').setValue('app.js')
  await wrapper.find('[aria-label="粘贴导入内容"]').setValue('fetch("/relative")')
  await wrapper.find('button').trigger('click'); await flushPromises()
  expect(JSON.parse(global.fetch.mock.calls[0][1].body)).toMatchObject({resource_url:'https://portal.example.test/app.js',name:'app.js'})
  expect(wrapper.emitted('staged')[0][0].parser).toBe('js-manifest')
  wrapper.unmount()
})

it('shows bounded change totals and opens the selected endpoint', async () => {
  const Changes = (await import('../src/views/ChangesView.vue')).default
  global.fetch = vi.fn(async () => response({counts:{new:4,changed:2,facts:30},rows:[{id:12,kind:'endpoint',display_key:'GET /orders',is_new:true,fact_count:3}],next_cursor:'more'}))
  const wrapper = mount(Changes); await flushPromises()
  expect(wrapper.text()).toContain('新增 4')
  await wrapper.find('.change-row').trigger('click')
  expect(store.workspaceJump.category).toBe('api')
  expect(store.workspaceJump.row.id).toBe(12)
  wrapper.unmount()
})
