/**
 * 目录浏览框（FsPicker）+ 导入对话框的「浏览…」。
 *
 * 为什么这条要有：`npm run build` 只能证明**编译**通过，而浏览框的全部价值
 * 在交互里 —— 点目录是「进去」、点文件是「带走」、根视图没有「选择此文件夹」。
 * 这个仓库没有浏览器可以点开看，挂起来跑一遍是最接近"打开页面"的动作。
 *
 * 假后端必须按 `GET /api/v1/fs` 的**真实形状**喂（`path` / `parent` /
 * `entries[].{name,path,is_dir}`）—— 形状错了测试就白测了。
 */

import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import FsPicker from '../src/components/FsPicker.vue'
import ImportDialog from '../src/views/ImportDialog.vue'

function fsResponse(payload) {
  return {
    ok: true, status: 200,
    json: async () => ({ ok: true, data: payload, meta: {} }),
  }
}

const ROOTS = { path: '', parent: null, entries: [
  { name: 'C:\\', path: 'C:\\', is_dir: true },
  { name: 'D:\\', path: 'D:\\', is_dir: true },
] }

const DRIVE_D = { path: 'D:\\', parent: null, entries: [
  { name: 'recon', path: 'D:\\recon', is_dir: true },
  { name: 'notes.txt', path: 'D:\\notes.txt', is_dir: false },
] }

const RECON = { path: 'D:\\recon', parent: 'D:\\', entries: [
  { name: 'sub', path: 'D:\\recon\\sub', is_dir: true },
  { name: 'subfinder.txt', path: 'D:\\recon\\subfinder.txt', is_dir: false },
] }

/** 按 query 里的 path 分叉的假 `/fs`。记录每次被问到的 URL。 */
function stubFs(seen) {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
    const href = String(url)
    seen.push(href)
    const path = new URL(href, 'http://localhost').searchParams.get('path')
    if (!path) return fsResponse(ROOTS)
    if (path === 'D:\\') return fsResponse(DRIVE_D)
    if (path === 'D:\\recon') return fsResponse(RECON)
    return { ok: false, status: 404,
             json: async () => ({ ok: false,
               error: { code: 'E_DIR_NOT_FOUND', message: '路径不存在：' + path } }) }
  })
}

afterEach(() => vi.restoreAllMocks())

describe('目录浏览框', () => {
  it('起步是根视图：列盘符，「选择此文件夹」与「上一级」都还不能用', async () => {
    const seen = []
    stubFs(seen)
    const wrapper = mount(FsPicker)
    await vi.waitFor(() => expect(wrapper.text()).toContain('C:\\'))
    expect(wrapper.text()).toContain('D:\\')
    expect(seen[0]).not.toContain('?path=')          // 没带 path = 问根
    const buttons = wrapper.findAll('button')
    const choose = buttons.find((b) => b.text() === '选择此文件夹')
    const up = buttons.find((b) => b.text() === '上一级')
    expect(choose.attributes('disabled')).toBeDefined()
    expect(up.attributes('disabled')).toBeDefined()
  })

  it('点目录是「进去」（再问一次 /fs），点文件是「带走」（emit pick 绝对路径）', async () => {
    const seen = []
    stubFs(seen)
    const wrapper = mount(FsPicker)
    await vi.waitFor(() => expect(wrapper.text()).toContain('D:\\'))

    await wrapper.findAll('.row')[1].trigger('click')        // 根 → D:\
    await vi.waitFor(() => expect(wrapper.text()).toContain('recon'))
    expect(seen.some((href) => href.includes('path=D%3A%5C'))).toBe(true)
    // 进去之后「选择此文件夹」可用了（选的是**当前**目录）
    const choose = wrapper.findAll('button')
      .find((b) => b.text() === '选择此文件夹')
    expect(choose.attributes('disabled')).toBeUndefined()

    await wrapper.findAll('.row')[0].trigger('click')        // D:\ → D:\recon
    await vi.waitFor(() => expect(wrapper.text()).toContain('subfinder.txt'))

    await wrapper.findAll('.row')[1].trigger('click')        // 点文件
    expect(wrapper.emitted('pick')).toEqual([['D:\\recon\\subfinder.txt']])
  })

  it('「选择此文件夹」emit 当前目录；请求失败时错误**响亮地**显示', async () => {
    const seen = []
    stubFs(seen)
    const wrapper = mount(FsPicker)
    await vi.waitFor(() => expect(wrapper.text()).toContain('D:\\'))
    await wrapper.findAll('.row')[1].trigger('click')        // D:\
    await vi.waitFor(() => expect(wrapper.text()).toContain('recon'))
    await wrapper.findAll('.row')[0].trigger('click')        // D:\recon
    await vi.waitFor(() => expect(wrapper.text()).toContain('subfinder.txt'))

    const choose = wrapper.findAll('button')
      .find((b) => b.text() === '选择此文件夹')
    await choose.trigger('click')
    expect(wrapper.emitted('pick')).toEqual([['D:\\recon']])

    // 没夹具的路径 → 404 要说出来，而不是变成一个"空目录"
    await wrapper.findAll('.row')[0].trigger('click')        // 进 sub → 假后端没有
    await vi.waitFor(() => expect(wrapper.text()).toContain('E_DIR_NOT_FOUND'))
  })
})

describe('导入对话框的「服务端目录…（高级）」', () => {
  it('选中的路径追加进表单，浏览框关闭', async () => {
    stubFs([])
    const wrapper = mount(ImportDialog, { attachTo: document.body })
    expect(wrapper.find('textarea').element.value).toBe('')

    await wrapper.findAll('button')
      .find((b) => b.text().includes('服务端目录')).trigger('click')
    const picker = wrapper.findComponent(FsPicker)
    expect(picker.exists()).toBe(true)

    picker.vm.$emit('pick', 'D:\\recon\\subfinder.txt')
    await wrapper.vm.$nextTick()
    expect(wrapper.find('textarea').element.value)
      .toBe('D:\\recon\\subfinder.txt')
    expect(wrapper.findComponent(FsPicker).exists()).toBe(false)
  })

  it('重复选同一个路径不加第二行（同一个文件导两遍是纯坏行来源）', async () => {
    stubFs([])
    const wrapper = mount(ImportDialog, { attachTo: document.body })
    await wrapper.findAll('button')
      .find((b) => b.text().includes('服务端目录')).trigger('click')
    wrapper.findComponent(FsPicker).vm.$emit('pick', 'D:\\recon\\subfinder.txt')
    await wrapper.vm.$nextTick()
    await wrapper.findAll('button')
      .find((b) => b.text().includes('服务端目录')).trigger('click')
    wrapper.findComponent(FsPicker).vm.$emit('pick', 'D:\\recon\\subfinder.txt')
    await wrapper.vm.$nextTick()
    expect(wrapper.find('textarea').element.value)
      .toBe('D:\\recon\\subfinder.txt')
  })
})
