"""GUI 静态资源真的被服务出来了（2.8 任务 1 的验收面）。

**为什么这条测试不能省**：`check.py` 的 R7-ASSETS 扫的是"产物里有没有外部
域名"，而一个**空的或缺失的** `api/web/` 目录在那条规则下是**通过**的
（"目录不存在不算问题"）。于是"前端构建忘了跑 / 产物没提交 / `base` 配错"
这三种情况都会让 R7-ASSETS 绿着，而用户打开 `magpie app` 打印的那条 URL 只会
看到一张白页 —— 白页的报错通常出现在浏览器控制台里，而那是看日志的人看不到
的地方。

**已知边界**：这里验的是"服务交出去的字节是对的"，不是"浏览器把它渲染对了"。
前端自己的行为由 `frontend/tests/**`（`npm test`）守 —— 那需要一个 JS 运行时，
而 `check.py` 不该依赖 node 装没装。两条测试各守一段，谁也不冒充谁。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from magpie.api.app import WEB_DIR, create_app

TOKEN = "web-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

_ASSET_RE = re.compile(r'(?:src|href)="(/web/[^"]+)"')


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=tmp_path / "magpie.db", data_dir=tmp_path / "data",
                     token=TOKEN)
    with TestClient(app) as test_client:
        yield test_client


def test_web_dir_has_a_built_page():
    assert WEB_DIR.is_dir(), f"静态资源目录不存在：{WEB_DIR}（前端构建没跑？）"
    assert (WEB_DIR / "index.html").is_file(), "缺少 index.html"


def test_root_serves_the_built_page(client):
    response = client.get("/")
    assert response.status_code == 200
    html = response.text
    assets = _ASSET_RE.findall(html)
    assert assets, ("index.html 没有引用 /web/ 下的任何资源 —— 多半是 Vite 的 "
                    "`base` 没配成 /web/，而那是**静默白屏**的那一类故障")


def test_referenced_assets_are_actually_served(client):
    """引用了但取不到 = 白页。所以每一条引用都要真的请求一次。"""
    html = client.get("/").text
    for asset in _ASSET_RE.findall(html):
        response = client.get(asset)
        assert response.status_code == 200, f"{asset} 取不到（{response.status_code}）"
        assert response.content, f"{asset} 是空的"


def test_index_is_not_cacheable(client):
    """GUI 是本地单用户：刷新页面必须拿到**当前**那份产物。

    一个被缓存住的旧 index.html 会去引用一个已经不存在的 `index-<hash>.js`，
    而表现同样是白屏 —— 与"产物没构建"是同一个症状、同一个查法。
    """
    response = client.get("/")
    assert response.status_code == 200
    # 允许 no-store / no-cache / max-age=0 —— 但**不允许**一个正数的 max-age
    cache_control = response.headers.get("cache-control", "no-store")
    match = re.search(r"max-age=(\d+)", cache_control)
    assert match is None or int(match.group(1)) == 0, \
        f"index.html 被标成可缓存 {cache_control!r} —— 换一次构建就会白屏"


def test_assets_dir_is_local_only(client):
    """产物里的每一条引用都必须落在 /web/ 下（相对路径，不是外部地址）。"""
    html = client.get("/").text
    for ref in re.findall(r'(?:src|href)="([^"]+)"', html):
        assert ref.startswith("/web/"), f"静态引用不是本地路径：{ref!r}"


def test_missing_web_dir_gives_a_loud_500(tmp_path, monkeypatch):
    """目录真的没了时，`/` 必须**响亮地**报错，而不是返回一张白页。

    这一条是给"打包漏了 web 目录"准备的：那是一个发布事故，而发布事故最怕的
    是被静默成一个看起来像前端 bug 的白屏。
    """
    from magpie.api import app as app_module

    missing = tmp_path / "nope"
    monkeypatch.setattr(app_module, "WEB_DIR", missing)
    app = app_module.create_app(db_path=tmp_path / "magpie.db",
                                data_dir=tmp_path / "data", token=TOKEN)
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "E_NO_WEB"


def test_web_files_are_packaged():
    """`pyproject.toml` 的 package-data 必须**递归**覆盖 web/ 下的产物。

    2.7 那份写的是 `web/*.html` / `web/*.css` / `web/*.js` —— 平铺的。Vite 的
    产物在 `web/assets/` 下面，于是"本地装上之后 GUI 是空的"会一直等到有人
    `pip install` 一次才会被发现。断言读的是 `pyproject.toml` 本身。
    """
    text = (Path(__file__).resolve().parents[3] / "pyproject.toml").read_text(
        encoding="utf-8")
    section = text.split("[tool.setuptools.package-data]", 1)
    assert len(section) == 2, "pyproject.toml 里找不到 package-data 段"
    # 只看**赋值那一行**。第一版写成 `assert "web/**/*" in section[1]`，而那段
    # 注释里恰好写着「届时这里改成递归的 "web/**/*"」—— 于是这条断言在
    # package-data 还是平铺的时候就**绿了**。一条被注释满足的断言，比没有断言
    # 更坏：它让人以为验过了。
    assignment = [line for line in section[1].splitlines()
                  if line.strip().startswith('"magpie.api"')]
    assert len(assignment) == 1, f"package-data 里找不到 magpie.api 的赋值：{assignment}"
    assert "web/**/*" in assignment[0], \
        "package-data 没有递归覆盖 web/ —— 构建产物在 web/assets/ 下，平铺的模式收不到它"
