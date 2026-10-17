/**
 * Vite 配置（2.8 任务 1）。
 *
 * 三件事在这里定下来，每一件都有理由：
 *
 *   1. **`base: '/web/'`** —— 产物由 FastAPI 挂在 `/web` 上（`api/app.py` 的
 *      `app.mount("/web", StaticFiles(...))`），`/` 单独返回 index.html。
 *      用默认的 `/` 会让构建后的 `<script src="/assets/…">` 打到 404 上，
 *      而页面**不报错**，只是白屏 —— 那种故障最难查。
 *
 *   2. **产物直接落进 `magpie/api/web/`**，那正是 `pyproject.toml` 的
 *      package-data 与 `app.WEB_DIR` 指向的目录。构建一次，Python 包与本地
 *      服务同时拿到它；没有"构建完还要记得拷过去"这一步，也就没有"忘了拷"
 *      这种状态。（`emptyOutDir` 会清掉 2.7 那个手写的占位页 —— 它本来就写着
 *      "2.8 的 Vite 构建产物会替换它"。）
 *
 *   3. **不带 sourcemap、不做压缩体积报告** —— sourcemap 会把源码（包括注释里
 *      的绝对路径）打包进产物，而产物是要随包发布的。体积报告只影响构建日志，
 *      不影响产物，关掉它让构建安静一点。
 *
 * 目标 `es2020`：这个页面只在自己机器上的最新浏览器里跑，不需要为老浏览器
 * 降级；把目标定新一点换来的是**更小的产物**，而首屏预算就那么多。
 */
import { fileURLToPath } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

/**
 * 把 Vue 运行时里的**错误文档链接**换成本地标记。
 *
 * 为什么需要它：Vue 的生产构建在 `runtime-core` 里硬编码了一行
 *
 *     `https://vuejs.org/error-reference/#runtime-${type}`
 *
 * 它只出现在 `handleError` 的错误信息里，**不会被浏览器取回**。但计划书 §7.6
 * 与 2.8 的交付标准表写的都是"构建产物中**无任何外部域名引用**"，而
 * `check.py` 的 R7-ASSETS 是按**文本**扫的 —— 它扫到了，于是整轮验证变红。
 *
 * 三条路里选了第三条：
 *
 *   1. 把 `vuejs.org` 加进 R7-ASSETS 的白名单 —— **不行**。那等于为了让规则
 *      变绿而放宽规则，而且放开的是一个真实存在的外部域名。
 *   2. 把 URL 拆成 `"https:/" + "/vuejs.org/…"` 拼起来绕过扫描 —— **不行**。
 *      R7-ASSETS 的已知边界里明写了拼接能绕开静态扫描；**故意**用它就是把
 *      "扫不到"当成"没问题"，比不写这条规则更坏。
 *   3. **在构建时把它换掉**（这里）—— 产物里真的没有这个外部域名了，断言
 *      因此是真的，而不是被放过的。
 *
 * 换成什么：`vue-runtime-error-<type>`。原文案是给联网的人点进去看文档的，
 * 而这个工具是**离线自用**的 —— 一条需要联网才能打开的链接对它本来就没有用。
 * 换成一个能搜的短标记，比留一条点不开的 URL 更诚实。
 *
 * **已知边界**：这条替换只认 `@vue/runtime-core` 里那一行已知的字符串。它不
 * 试图做"通用 URL 剥离"—— 一个通用的剥离器会在某次依赖升级后静默地什么都不
 * 匹配，而这条断言会继续绿着。所以它不是防线，最后一道防线是
 * `check.py lint` 的 R7-ASSETS（它扫的是**产物**，不管上游怎么变）。
 */
function localVueErrorReference() {
  const EXTERNAL = /https:\/\/vuejs\.org\/error-reference\/#runtime-\$\{type\}/g
  const LOCAL = 'vue-runtime-error-${type}'
  return {
    name: 'magpie:local-vue-error-reference',
    enforce: 'pre',
    transform(code, id) {
      if (!id.includes('runtime-core')) return null
      EXTERNAL.lastIndex = 0
      if (!EXTERNAL.test(code)) return null
      EXTERNAL.lastIndex = 0
      return { code: code.replace(EXTERNAL, LOCAL), map: null }
    },
  }
}

export default defineConfig({
  plugins: [localVueErrorReference(), vue()],
  base: '/web/',
  server: {
    host: '127.0.0.1',
    proxy: { '/api': `http://127.0.0.1:${process.env.MAGPIE_DEV_API_PORT || '8765'}` },
  },
  build: {
    outDir: fileURLToPath(new URL('../magpie/api/web', import.meta.url)),
    emptyOutDir: true,
    target: 'es2020',
    assetsDir: 'assets',
    sourcemap: false,
    reportCompressedSize: false,
    chunkSizeWarningLimit: 1200,
  },
  // 前端单测（`npm test`）。环境用 jsdom：挂载组件这件事需要一个 DOM，
  // 而这里要断言的恰恰是**挂载之后**的连接入口是关死的（只读）。
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.test.js'],
  },
})
