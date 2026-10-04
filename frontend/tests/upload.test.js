/**
 * 导入对话框的**原生选择器 + 拖拽上传**（0.17.0 的 `POST /api/v1/uploads`）。
 *
 * 这条链的价值全在交互里：drop 收到的是浏览器安全模型给的**文件内容**，
 * 上传换回**服务进程写的绝对路径**再填进表单 —— 测试钉住三件事：
 *
 *   1. drop / change 打到的确实是 `/api/v1/uploads`，且是 multipart
 *      （body 是 FormData，**不手设 Content-Type** —— 手写的头只会把浏览器
 *      生成的 boundary 弄坏，这与 JSON 请求头是两种相反的纪律）。
 *   2. 返回的 `files[].path` 追加进 textarea（复用 addPath 去重），**不自动
 *      提交** —— 表单选项照旧由用户确认后再点「开始导入」。
 *   3. 失败要响亮：错误显示出来、表单保持原样；上传中「开始导入」禁用。
 *
 * 假后端必须按 `POST /uploads` 的**真实形状**喂
 * （`{files: [{name, path, bytes}], count}`，信封 `{ok, data, meta}`）。
 */

import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import ImportDialog from '../src/views/ImportDialog.vue'

const STAGED =
  'D:\\magpie-data\\uploads\\20261004-101530-ab12\\subfinder.txt'

function uploadOk(files) {
  return {
    ok: true, status: 201,
    json: async () => ({
      ok: true,
      data: { files, count: files.length },
      meta: {},
    }),
  }
}

function uploadFail(code, message) {
  return {
    ok: false, status: 413,
    json: async () => ({ ok: false, error: { code, message } }),
  }
}

function spyFetch(impl) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(impl)
}

afterEach(() => vi.restoreAllMocks())

describe('导入对话框：拖拽上传', () => {
  it('drop 文件 → POST /api/v1/uploads → 绝对路径填进表单', async () => {
    const seen = []
    spyFetch(async (url, options) => {
      seen.push({ url: String(url), options })
      return uploadOk([{ name: 'subfinder.txt', path: STAGED, bytes: 14 }])
    })
    const wrapper = mount(ImportDialog, { attachTo: document.body })
    const drop = wrapper.find('.drop')
    expect(drop.exists()).toBe(true)

    await drop.trigger('drop', {
      dataTransfer: { files: [new File(['a.example.com'], 'subfinder.txt')] },
    })
    await vi.waitFor(() =>
      expect(wrapper.find('textarea').element.value).toBe(STAGED))

    expect(seen).toHaveLength(1)
    expect(seen[0].url).toBe('/api/v1/uploads')
    expect(seen[0].options.method).toBe('POST')
    expect(seen[0].options.body).toBeInstanceOf(FormData)
    // 不手设 Content-Type —— multipart boundary 必须由浏览器生成
    expect(seen[0].options.headers['Content-Type']).toBeUndefined()
    // 路径只是**填进表单**，没有替用户提交导入
    expect(wrapper.text()).toContain('开始导入')
  })

  it('同一批多个文件全部填入；已有的路径不重复加行', async () => {
    const fetchMock = spyFetch(async () => uploadOk([
      { name: 'a.txt', path: 'D:\\staging\\a.txt', bytes: 1 },
      { name: 'b.txt', path: 'D:\\staging\\b.txt', bytes: 1 },
      { name: 'a.txt', path: 'D:\\staging\\a-1.txt', bytes: 1 }, // 服务端已去重
    ]))
    const wrapper = mount(ImportDialog, { attachTo: document.body })
    await wrapper.find('.drop').trigger('drop', {
      dataTransfer: { files: [new File(['1'], 'a.txt')] },
    })
    await vi.waitFor(() =>
      expect(wrapper.find('textarea').element.value.split('\n')).toHaveLength(3))

    // 再拖一批返回同样的路径：addPath 去重，行数不长 —— 完成与否用
    // phases 行数钉（消息文本两批相同，靠文本等完成会立刻误判）
    await wrapper.find('.drop').trigger('drop', {
      dataTransfer: { files: [new File(['1'], 'a.txt')] },
    })
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    await vi.waitFor(() =>
      expect(wrapper.findAll('.phases li')).toHaveLength(2))
    expect(wrapper.find('textarea').element.value.split('\n'))
      .toHaveLength(3)
  })

  it('上传失败要响亮：错误显示、表单保持原样', async () => {
    spyFetch(async () => uploadFail('E_UPLOAD_TOO_LARGE', '超过单文件上限'))
    const wrapper = mount(ImportDialog, { attachTo: document.body })
    await wrapper.find('.drop').trigger('drop', {
      dataTransfer: { files: [new File(['x'.repeat(10)], 'huge.pcap')] },
    })
    await vi.waitFor(() => expect(wrapper.text()).toContain('上传失败'))
    expect(wrapper.text()).toContain('E_UPLOAD_TOO_LARGE')
    expect(wrapper.find('textarea').element.value).toBe('')
  })

  it('上传进行中：按钮报进度、提交禁用；完成后解锁', async () => {
    let release = null
    const pending = new Promise((resolve) => { release = resolve })
    spyFetch(() => pending)
    const wrapper = mount(ImportDialog, { attachTo: document.body })
    const submit = () => wrapper.findAll('button')
      .find((b) => b.text().includes('开始导入'))

    await wrapper.find('.drop').trigger('drop', {
      dataTransfer: { files: [new File(['1'], 'a.txt')] },
    })
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('正在上传 1 个文件')
    expect(submit().attributes('disabled')).toBeDefined()

    release(uploadOk([{ name: 'a.txt', path: 'D:\\staging\\a.txt', bytes: 1 }]))
    await vi.waitFor(() =>
      expect(submit().attributes('disabled')).toBeUndefined())
    expect(wrapper.find('textarea').element.value)
      .toBe('D:\\staging\\a.txt')
  })
})

describe('导入对话框：原生选择器', () => {
  it('隐藏的 file input 是 multiple，change 之后路径同样填进表单', async () => {
    spyFetch(async () =>
      uploadOk([{ name: 'httpx.jsonl', path: 'D:\\staging\\httpx.jsonl',
                  bytes: 5 }]))
    const wrapper = mount(ImportDialog, { attachTo: document.body })
    const input = wrapper.find('input[type="file"]')
    expect(input.exists()).toBe(true)
    expect(input.attributes('multiple')).toBeDefined()

    // jsdom 没有系统对话框 —— 直接喂 files 再触发 change（这就是 change 的全部输入）
    Object.defineProperty(input.element, 'files', {
      value: [new File(['{}'], 'httpx.jsonl')], configurable: true,
    })
    await input.trigger('change')
    await vi.waitFor(() =>
      expect(wrapper.find('textarea').element.value)
        .toBe('D:\\staging\\httpx.jsonl'))
    // value 已清空 —— 同一个文件再选一次也会触发 change
    expect(input.element.value).toBe('')
  })
})
