"""契约：`openapi.json` 快照（2.7 交付标准表的第一行）。

**为什么这条测试比它看起来重要**: `/api/v1` 是**对外契约**（GUI、CLI、MCP
三个消费者共用它）。而 `openapi.json` 是自动生成的 —— 一次 FastAPI / pydantic
升级、一次顺手改的参数名，都会**静默地**改变它，而三个消费者里可能只有一个
跟着改了。快照测试把"静默"变成"响亮"：变了就红，红了你必须做一次有意的决定
（改快照 = 承认这是契约变更 → 该升 minor 就升，该写 CHANGELOG 就写）。

**故意不钉 FastAPI 版本**: 钉住版本能让快照更"稳"，但也把上面这个信号一起
钉死了 —— 升级带来的契约漂移会一直藏着，直到某天在别处爆炸。让快照在升级时
红一次，是这套机制**本来**要买的东西。

重新生成（只在你确认过这次变更是有意的之后）:

    MAGPIE_OPENAPI_REGEN=1 py -3.13 -m pytest magpie/api/tests/test_contract_openapi.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

from magpie.api.app import create_app

SNAPSHOT = Path(__file__).resolve().parent / "openapi_snapshot.json"

#: 生成快照时**必须**与真实服务一致的两件事：路由前缀与 openapi 路径。
#: 不改成别的值 —— 快照要描述的是真跑起来的那个 app。
_FIXED = {"token": "snapshot-token"}


def _generate() -> dict:
    """在**临时目录**里装配一个 app 并取它的 openapi。

    不用 `:memory:`：写线程会在它自己的那条 :memory: 连接上建库，而读请求各拿
    一条**新的** :memory: 连接（各自一个新空库）—— 那种"能跑但什么都没连上"的
    状态是最不像故障的故障。落一个临时文件，读写才是同一个库。
    """
    import tempfile
    with tempfile.TemporaryDirectory(prefix="magpie-openapi-") as tmp:
        app = create_app(db_path=Path(tmp) / "magpie.db", token=_FIXED["token"])
        with TestClient(app) as client:
            response = client.get("/openapi.json")
            assert response.status_code == 200
            return response.json()


def test_openapi_matches_snapshot():
    current = _generate()
    if os.environ.get("MAGPIE_OPENAPI_REGEN") == "1" or not SNAPSHOT.is_file():
        SNAPSHOT.write_text(json.dumps(current, ensure_ascii=False, indent=2,
                                       sort_keys=True) + "\n", encoding="utf-8")
        if os.environ.get("MAGPIE_OPENAPI_REGEN") == "1":
            return
        raise AssertionError(
            f"快照不存在，已生成 {SNAPSHOT.name}。请检查它的内容并提交 —— "
            f"这份文件就是对外的 `/api/v1` 契约。")

    saved = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    if saved != current:
        saved_paths = set(saved.get("paths") or {})
        current_paths = set(current.get("paths") or {})
        added = sorted(current_paths - saved_paths)
        removed = sorted(saved_paths - current_paths)
        changed = sorted(p for p in saved_paths & current_paths
                         if saved["paths"][p] != current["paths"][p])
        raise AssertionError(
            "openapi.json 与快照不一致 —— 这是**有意变更**吗？\n"
            f"  新增路由：{added}\n  删除路由：{removed}\n  改动路由：{changed}\n"
            f"确认无误后：MAGPIE_OPENAPI_REGEN=1 py -3.13 -m pytest "
            f"magpie/api/tests/test_contract_openapi.py，"
            f"并按需升 minor + 写 CHANGELOG。")
