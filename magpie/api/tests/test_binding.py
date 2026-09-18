"""绑定与安全：只监听 `127.0.0.1`，禁止 `0.0.0.0`，不开 CORS 通配（2.7 交付标准）。

三条断言的分工（缺一条就会留下一个"看起来验过了"的洞）:

  1. **运行时**: `create_app(host=...)` / `serve(host=...)` 收到非回环地址必须抛错。
     这是唯一能挡住"某天有人为了远程调试临时加个参数"的断言。
  2. **静态**: 全仓源码里不得出现把 `0.0.0.0` 当绑定地址的字面量。
     运行时的断言挡不住"把默认值改了"—— 改默认值的人不会看到它抛错。
  3. **响应头**: 不带 `Access-Control-Allow-Origin: *`。不加 CORS 中间件是**默认**
     行为，而"默认行为"没有测试守着就会在第一次"跨域取不到数据"时被顺手加上。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from magpie.api.app import create_app, serve
from magpie.api.auth import is_loopback

ROOT = Path(__file__).resolve().parents[3]
API_DIR = ROOT / "magpie" / "api"


def test_loopback_predicate():
    assert is_loopback("127.0.0.1")
    assert is_loopback("::1")
    assert is_loopback("localhost")
    for bad in ("0.0.0.0", "192.168.1.10", "", "127.0.0.1.evil.com"):
        assert not is_loopback(bad), f"{bad!r} 不该被当成回环地址"


def test_create_app_refuses_non_loopback():
    with pytest.raises(ValueError) as excinfo:
        create_app(db_path="x.db", host="0.0.0.0")
    assert "0.0.0.0" in str(excinfo.value)
    assert "回环" in str(excinfo.value)


def test_serve_refuses_non_loopback(tmp_path):
    # 绑定都没发生就该抛 —— 校验放在"请求进来之后"没有意义（那时端口已经开着）。
    with pytest.raises(ValueError):
        serve(db_path=tmp_path / "magpie.db", host="0.0.0.0", open_browser=False)


def test_no_wildcard_cors_header(tmp_path):
    app = create_app(db_path=tmp_path / "magpie.db", token="t")
    with TestClient(app) as client:
        response = client.get("/api/v1/meta", headers={
            "Authorization": "Bearer t", "Origin": "http://evil.example"})
        assert response.status_code == 200
        assert "access-control-allow-origin" not in {
            k.lower() for k in response.headers}
        # 连预检也不该被放行
        preflight = client.options("/api/v1/meta", headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "GET"})
        assert "access-control-allow-origin" not in {
            k.lower() for k in preflight.headers}


def _docstring_nodes(tree) -> set:
    """所有文档字符串节点的 id。

    必须排除它们：模块说明里写着"绑 0.0.0.0 等于把数据放到局域网上"这种**解释**，
    而它在 AST 里与真正的绑定字面量长得一模一样（都是 str Constant）。按用途
    白名单（"这行有没有'拒绝'二字"）会随措辞改动静默失效；按 AST 结构排除
    文档字符串则是稳定的。
    """
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _bind_host_literals(path: Path) -> list[tuple[str, int]]:
    """源码里所有"像绑定地址"的字符串字面量。

    认两种形态：`host=` 关键字实参，以及裸的 `"0.0.0.0"` / `"::"` 字面量
    （后者覆盖 `uvicorn.run(app, "0.0.0.0", ...)` 那种位置实参写法）。

    **已知边界**：拼出来的地址（`"0.0." + "0.0"`）认不出 —— 这种只能靠
    code review，规则不假装覆盖。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = _docstring_nodes(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            if node.value in ("0.0.0.0", "::"):
                found.append((node.value, node.lineno))
        elif isinstance(node, ast.keyword) and node.arg == "host" \
                and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str) \
                and not is_loopback(node.value.value):
            found.append((node.value.value, node.value.lineno))
    return found


def test_no_non_loopback_bind_literal_in_sources():
    """`magpie/api/**`（不含测试）里不得出现把服务绑到非回环地址的字面量。

    **为什么静态扫一遍**: 运行时的断言挡得住"调用时传了 0.0.0.0"，挡不住
    "把默认值改成了 0.0.0.0" —— 改默认值的人不会看到任何异常，只有下一个
    启动服务的人会。测试文件排除在外：它们的反例字符串正是这条规则的**素材**。
    """
    offenders = []
    for path in sorted(API_DIR.rglob("*.py")):
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue
        for literal, lineno in _bind_host_literals(path):
            offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {literal!r}")
    assert not offenders, (
        "发现非回环绑定地址 —— 计划书 §7.6 的硬约束：\n" + "\n".join(offenders))


def test_selfcheck_bind_scanner():
    """反例自检：扫描器必须真的会报，也必须不误报。

    没有这一段，"没有违规"与"扫描器坏了"是同一个结果 —— 那和没写没有区别。
    """
    import tempfile
    cases = {
        "keyword": ('serve(host="0.0.0.0")\n', True),
        "positional": ('uvicorn.run(app, "0.0.0.0", 8000)\n', True),
        "ipv6-any": ('serve(host="::")\n', True),
        "docstring": ('"""说明：不要绑 0.0.0.0。"""\nserve(host="127.0.0.1")\n', False),
        "loopback": ('serve(host="127.0.0.1")\n', False),
    }
    with tempfile.TemporaryDirectory() as tmp:
        for name, (source, should_flag) in cases.items():
            path = Path(tmp) / f"{name}.py"
            path.write_text(source, encoding="utf-8")
            flagged = bool(_bind_host_literals(path))
            assert flagged == should_flag, (
                f"自检失败 [{name}]：期望 {'报' if should_flag else '不报'}，"
                f"实际 {'报' if flagged else '不报'} —— 源：{source!r}")
