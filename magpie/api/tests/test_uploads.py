"""上传 `POST /api/v1/uploads` —— 原生选择器 / 拖拽上传的后端。

断言盯住三件事（与 `/fs` 那份同一套契约观）：

  * **绝对路径**：返回的 `files[].path` 必须能直接填进 `POST /imports` ——
    浏览器给的内容与服务进程写的路径必须出自同一个进程（`/fs` 收口那条
    `test_browse_then_import_agree_on_the_same_path` 的上传版）。
  * **一批原子**：要么带回完整的一批，要么什么都别留 —— 半截文件看起来像
    一个可以导入的文件，比失败更糟。
  * **失败要响亮**：没带字段 400、超限 413，都带 code，而不是空的 200。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from magpie.api import routes
from magpie.api.app import create_app

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=tmp_path / "magpie.db", data_dir=tmp_path / "data",
                     token=TOKEN)
    with TestClient(app) as test_client:
        yield test_client


def _uploads_root(tmp_path) -> Path:
    return tmp_path / "data" / "uploads"


def test_requires_token(client):
    assert client.post("/api/v1/uploads").status_code == 401


def test_no_files_field_is_400(client):
    response = client.post("/api/v1/uploads", headers=HEADERS)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "E_NO_FILES"
    assert error["hint"]


def test_stages_files_and_returns_absolute_paths(client, tmp_path):
    response = client.post(
        "/api/v1/uploads", headers=HEADERS,
        files=[("files", ("a.txt", b"alpha\n", "text/plain")),
               ("files", ("b.txt", b"beta\n", "text/plain"))])
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert set(data) == {"files", "count"}           # 响应形状钉死
    assert data["count"] == 2
    assert [item["name"] for item in data["files"]] == ["a.txt", "b.txt"]

    root = _uploads_root(tmp_path)
    for item, raw in zip(data["files"], (b"alpha\n", b"beta\n")):
        assert set(item) == {"name", "path", "bytes"}
        path = Path(item["path"])
        assert path.is_absolute()
        assert path.parent.parent == root            # uploads/<批>/<文件>
        assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", path.parent.name)
        assert path.read_bytes() == raw
        assert item["bytes"] == len(raw)


def test_uploaded_path_feeds_imports(client):
    """与 /fs 收口同一条：上传拿到的路径，`POST /imports` 必须认。"""
    response = client.post(
        "/api/v1/uploads", headers=HEADERS,
        files=[("files", ("subfinder.txt", b"a.example.com\n", "text/plain"))])
    assert response.status_code == 201, response.text
    file_path = response.json()["data"]["files"][0]["path"]
    assert Path(file_path).exists()                  # 快速失败判据与这里一致
    client.post("/api/v1/projects", headers=HEADERS, json={"name": "demo"})
    accepted = client.post("/api/v1/imports", headers=HEADERS,
                           json={"paths": [file_path], "project": "demo"})
    assert accepted.status_code == 202, accepted.text


def test_filename_is_washed_to_basename(client):
    """路径穿越与 Windows 习惯都只留 basename；`.` / `..` 落回 file.bin。"""
    response = client.post(
        "/api/v1/uploads", headers=HEADERS,
        files=[("files", ("../../evil.txt", b"1", "text/plain")),
               ("files", (r"C:\temp\note.txt", b"2", "text/plain")),
               ("files", ("..", b"3", "text/plain"))])
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert [item["name"] for item in data["files"]] == \
        ["evil.txt", "note.txt", "file.bin"]
    for item in data["files"]:
        path = Path(item["path"])
        assert path.name == item["name"]             # 落盘名 ≡ 返回名
        assert path.parent.parent.name == "uploads"  # 且真的在暂存批里


def test_casefold_dedupe_never_overwrites(client):
    """Windows 文件系统大小写不敏感：A.txt 与 a.txt 不许互相覆盖。"""
    response = client.post(
        "/api/v1/uploads", headers=HEADERS,
        files=[("files", ("A.txt", b"first", "text/plain")),
               ("files", ("a.txt", b"second", "text/plain"))])
    assert response.status_code == 201, response.text
    files = response.json()["data"]["files"]
    assert [item["name"] for item in files] == ["A.txt", "a-1.txt"]
    assert Path(files[0]["path"]).read_bytes() == b"first"
    assert Path(files[1]["path"]).read_bytes() == b"second"


def test_too_many_files_is_400(client, monkeypatch):
    monkeypatch.setattr(routes, "UPLOAD_MAX_FILES", 1)
    response = client.post(
        "/api/v1/uploads", headers=HEADERS,
        files=[("files", ("a.txt", b"1", "text/plain")),
               ("files", ("b.txt", b"2", "text/plain"))])
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "E_TOO_MANY_FILES"


def test_oversize_rolls_back_the_whole_batch(client, tmp_path, monkeypatch):
    """超限即 413，且整批回滚 —— 失败的上传不留任何半截文件。"""
    monkeypatch.setattr(routes, "UPLOAD_MAX_BYTES", 16)
    response = client.post(
        "/api/v1/uploads", headers=HEADERS,
        files=[("files", ("ok.txt", b"small", "text/plain")),
               ("files", ("huge.pcap", b"x" * 64, "application/octet-stream"))])
    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == "E_UPLOAD_TOO_LARGE"
    assert error["hint"]                            # 指回「服务端目录…」那条路
    root = _uploads_root(tmp_path)
    assert root.is_dir()
    assert list(root.iterdir()) == [], "超限留下了残批"


def test_prune_removes_stale_batches_only(client, tmp_path):
    """入口顺手清 7 天前的暂存批；新鲜的批原样留着（还要喂 POST /imports）。"""
    root = _uploads_root(tmp_path)
    stale = root / "20200101-000000-dead"
    stale.mkdir(parents=True)
    (stale / "old.txt").write_text("old", encoding="utf-8")
    long_ago = 1_600_000_000                        # 2020-09-13，远早于 7 天前
    os.utime(stale, (long_ago, long_ago))

    response = client.post(
        "/api/v1/uploads", headers=HEADERS,
        files=[("files", ("fresh.txt", b"new", "text/plain"))])
    assert response.status_code == 201, response.text
    assert not stale.exists(), "过期暂存批没有被清理"
    fresh = Path(response.json()["data"]["files"][0]["path"])
    assert fresh.exists()
