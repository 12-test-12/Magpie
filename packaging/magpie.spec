# -*- mode: python ; coding: utf-8 -*-
"""Magpie 的 PyInstaller **onedir** 打包（LARI-48）。

为什么 onedir 不 onefile：FastAPI / uvicorn 场景下 onefile 每次启动都要解压到
临时目录 —— 冷启动慢、杀软误报率高，而且"文件就在那里、可以打开看"这件事在
排障时是真有用的。判据点名首选 onedir，本 spec 就按它写。

## 三份仓库根数据：缺任何一份，产物都能编译过、却在运行时才炸

    schema/           magpie/core/migrate.py     SCHEMA_DIR = parents[2]/schema
                                                 magpie/core/store.py  同
    fingerprints/     .../_fingerprints.py       RULES_DIR  = parents[3]/fingerprints
    magpie/api/web/   magpie/api/app.py          WEB_DIR    = parents[1]/web

三个 `parents[n]` 都相对模块的 `__file__`（冻结态 = `_internal/magpie/...`），
所以 dest 一律按原路径落在 `_internal/<目录>` —— parents 才算得出同一个根。

## 动态发现的模块：静态 import 图看不见，必须显式收

    magpie.core.parsers  ingest.load_parsers() 用 pkgutil.iter_modules 扫包目录。
                         只收进 PYZ **不够**：冻结态的包目录若在文件系统上不
                         存在，iter_modules 扫到空 —— 产物里一个解析器都没有，
                         而 `magpie schema --json` 会安静地报 parsers: []。
                         所以解析器的 .py 源文件同时按**文件**落地（喂发现机制），
                         模本体由 hiddenimports 进 PYZ（真正被执行的那份）。
    uvicorn             config 用 importlib.import_module 按**字符串**取
                         loops / protocols / lifespan 的实现。
    magpie.adapters     ask.py 在函数体内惰性 import（R7 唯一出站点的伴侣）。
    yaml                pipeline / 指纹在函数体内惰性 import（extras 探测
                         find_spec 也要能找到它）。

构建期 pip 收集依赖不算运行时出站（判据 4）；产物的零外联由
packaging/smoke.sh 对**解压后**的产物断言。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

# 本 spec 在 packaging/ 下 —— SPECPATH 由 PyInstaller 提供（= spec 所在目录）。
ROOT = Path(SPECPATH).parent

hidden = (
    collect_submodules("magpie.core.parsers")   # pkgutil.iter_modules 的发现对象
    + collect_submodules("magpie.adapters")     # ask 的惰性 import
    + collect_submodules("uvicorn")             # 按字符串 import 的实现表
    + ["yaml"]                                  # pipeline / 指纹的惰性 import
)

# 解析器源文件**只取 *.py、平铺进目标目录** —— 用目录整拷会把 __pycache__
# 也带进包（体积 + 两份可能不同步的字节码）。
_parser_files = [
    (str(p), "magpie/core/parsers")
    for p in sorted((ROOT / "magpie" / "core" / "parsers").glob("*.py"))
]

datas = [
    (str(ROOT / "schema"), "schema"),
    (str(ROOT / "fingerprints"), "fingerprints"),
    (str(ROOT / "magpie" / "api" / "web"), "magpie/api/web"),
    *_parser_files,
]

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],                # 从仓库根找 magpie 包，不赌 site-packages 的解析顺序
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest"],               # dev 依赖不该出现在产物里（打包环境本就不装）
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,             # onedir：EXE 只出 bootloader，内容交给 COLLECT
    name="magpie",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,                      # CLI 要 stdout；`magpie app` 也在控制台打 URL
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="magpie",
)
