# Magpie GUI（前端源码）

Vue 3 + Vite 的只读探索画布（计划书 §7 / 2.8）。**这里的产物会落进
`magpie/api/web/`，并随 Python 包一起发布。**

```
frontend/               源码（这个目录）
  src/graph.js          画布纯逻辑：配色 / 聚合 / 布局 / 筛选（无 Vue、无 DOM）
  src/store.js          共享状态。**只读保证就落在这个文件上**（见它的文件头）
  src/api.js            /api/v1 的唯一客户端
  src/components/       外壳零件：顶部 / 左栏 / 卡片 / 画布 / 详情 / 抽屉 / 命令面板
  src/views/            页面：镜头 / 作业历史 / Inbox / 体检 / 导入
  tests/                单测（`npm test`）
magpie/api/web/         构建产物（**入库**，别手改）
```

## 常用命令

```bash
cd frontend
npm install          # 首次
npm run dev          # 开发（需同时跑 magpie app 提供服务端；见下方注意事项）
npm run build        # 构建 → magpie/api/web/
npm test             # 前端单测（聚合 / 布局 / 只读断言）
```

改完前端**必须重新构建并提交产物**：Python 侧的测试
（`magpie/api/tests/test_web.py`）与 `check.py lint` 的 R7-ASSETS 扫的都是
`magpie/api/web/` 里那一份，而不是这里。

## 三条不能破的纪律

1. **零出站**（计划书 §7.6）。没有 CDN、没有外部字体、没有远程图标 ——
   页面需要的一切都打进产物。`check.py` 的 R7-ASSETS 扫 `magpie/api/web/**`
   并让整轮验证为红。Vue 运行时里那条 `https://vuejs.org/error-reference/…`
   由 `vite.config.js` 的 `localVueErrorReference` 插件在构建时换成本地标记
   （那里写了为什么不选另外两条路）。
2. **探索模式只读**（§7.2，2.8 的交付标准之一）。画布的**内容**只能来自
   `GET /graph`：不能加节点、不能加边、不能删。三层一起守 ——
   `check.py lint` 的 READONLY 静态红线、`tests/readonly.test.js`、
   以及 `GraphCanvas.vue` / `EntityNode.vue` 里根本不存在那条入口。
   可编辑的是**将来的编排模式**，不是把这里放开。
3. **进度不自己数**。界面上的百分比、坏行数、阶段全部来自数据层的
   `on_progress`（经 SSE 推来）。界面自己记账迟早会出现"进度条说 80%，
   报告说 100%"，而两者都自称权威。

## 两次踩过的坑（省得下次再踩）

- **`import * as store` 不能用在要写 `store.x =` 的地方。** ESM 的命名空间
  对象属性是只读的，Rollup 会报 `Illegal reassignment of import "store"`，
  而构建**仍然产出文件** —— 只有那几行黄色的告警。用
  `import { store } from '../store.js'`。
- **本机 `node` 只对 MSYS shell 可见，`cmd` 找不到它。** `C:\nvm4w\nodejs`
  是一个 MSYS 风格的符号链接，`npm install` 里那些用 `cmd /c` 跑的
  postinstall 脚本会报 `'node' 不是内部或外部命令`。把真实目录放到 PATH
  前面即可：

  ```bash
  PATH="/c/Users/patter/AppData/Local/nvm/v22.19.0:$PATH" npm install
  ```
