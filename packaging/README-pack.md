# Magpie @VERSION@ · 可分发包

免安装：解压即用，**不需要**装 Python、不需要装 Node。
两个平台各一个包（`linux-x86_64` 的 `.tar.gz` / `windows-x86_64` 的 `.zip`），
解压后目录结构一致，入口都是 `magpie`（Windows 是 `magpie.exe`）。

> 整个 `magpie/` 目录一起搬 —— 可执行文件旁边的一切（运行库、GUI 静态资源、
> schema 迁移、指纹规则）都在 `_internal/` 里，只拷可执行文件跑不起来。

## 起服务（一条命令）

```bash
# Linux / macOS
./magpie app --no-browser

# Windows（PowerShell / CMD 里）
magpie.exe app --no-browser
```

会打印一行 `URL: http://127.0.0.1:<随机端口>/?token=<一次性 token>` ——
浏览器打开即可（`--no-browser` 时它只打印不开）。停止：`Ctrl-C`。

- **只绑回环**：服务只监听 `127.0.0.1`，这是硬约束不是参数（`--host 0.0.0.0`
  会被拒绝）。
- **数据位置**：默认在当前目录的 `.magpie/magpie.db`；用 `--data-dir <目录>`
  指定别处。第一次起服务会自动建库并应用全部 schema 迁移。

## 只读速查（不改任何数据）

```bash
./magpie --version          # 包是否活着
./magpie schema --json      # 类型 / 命令 / 枚举 / 解析器清单
./magpie ls --human         # 实体列表（需先有项目）
./magpie risk --json        # 风险评分与攻击面概览
./magpie doctor --json      # 数据体检
```

## 关于网络

这个工具本身**零出站**：不发遥测、不拉更新、页面不引 CDN。唯一例外是默认
关闭的 `magpie ask`（显式开启才出站，见 `magpie ask --help`）。
构建这台包时的 pip/npm 下载是构建行为，与运行时无关。

---

对应源码版本 `@VERSION@`；验证与手动打包流程分别见仓库 `.github/workflows/ci.yml`、`.github/workflows/delivery.yml`，使用说明见 `docs/ci-delivery.md`。
和 `packaging/`。
