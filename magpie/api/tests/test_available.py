"""`available()` —— extras.api 的探针（4.5 收口的两侧里"装"与"缺"各一条）。

`_run_app` 的可读报错说的一直是"FastAPI / uvicorn ……没有"—— 探针就必须把
这套 extras 的**每个模块**都问到：只问 fastapi 的话，fastapi 在、uvicorn 缺的
环境里它放行，然后在起服务时炸出裸 ImportError —— 报错文案与探针各说各话，
正是 4.5 要收掉的那类"两边长得一样其实只验了一半"。
"""

from __future__ import annotations

import magpie.api as api


def test_available_is_true_when_the_extras_are_installed():
    """extras **已装**时探针放行 —— 这是 `magpie app` 能真起服务的前提
    （真起服务的那半边由 api/tests 整套用例覆盖）。"""
    assert api.available() is True


def test_available_is_false_when_any_module_of_the_extras_is_missing(monkeypatch):
    """extras **缺任何一半**（这里模拟 uvicorn 缺席）→ 探针必须说"没有"，
    把 `magpie app` 引向那条带安装命令的可读报错，而不是放行到裸 ImportError。"""
    real = api.importlib.util.find_spec
    monkeypatch.setattr(api.importlib.util, "find_spec",
                        lambda name: None if name == "uvicorn" else real(name))
    assert api.available() is False
