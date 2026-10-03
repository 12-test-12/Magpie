"""目录浏览 `GET /api/v1/fs` —— 导入对话框「浏览…」的后端。

这条端点回答的只有一个问题：**"从这里能往哪走，走到头的路径长什么样"**。
所以断言盯住三件事：

  * **绝对路径**：`entries[].path` 必须能直接填进 `POST /imports` —— 浏览时
    看到的与提交后读到的必须是同一台机器上的同一个路径（routes 里那节注释
    的全部理由）。
  * **只列不读**：返回的是名字与 `is_dir`，没有文件内容、没有文件大小之外的
    任何东西 —— 读文件是导入的职责。
  * **失败要响亮**：路径不存在 404、拿文件当目录 400，都带 code，而不是
    空列表（空列表对 GUI 是"这里没东西"，而事实是"你给错了"）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from magpie.api.app import create_app

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=tmp_path / "magpie.db", data_dir=tmp_path / "data",
                     token=TOKEN)
    with TestClient(app) as test_client:
        yield test_client


def test_requires_token(client):
    assert client.get("/api/v1/fs").status_code == 401


def test_roots_without_path(client):
    """不给 path = 根视图：Windows 是存在的盘符，POSIX 是 `/`。"""
    response = client.get("/api/v1/fs", headers=HEADERS)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["path"] == ""
    assert data["parent"] is None
    # 至少一个根存在（Windows 上 C:\ 不可能缺席），且每个根都能"进去"
    assert data["entries"], "根列表为空 —— 没有可浏览的起点"
    assert all(item["is_dir"] for item in data["entries"])
    assert all(Path(item["path"]).is_absolute() for item in data["entries"])


def test_lists_entries_with_absolute_paths(client, tmp_path):
    target = tmp_path / "recon"
    (target / "sub").mkdir(parents=True)
    (target / "b.txt").write_text("x", encoding="utf-8")
    (target / "a.txt").write_text("y", encoding="utf-8")

    response = client.get("/api/v1/fs", headers=HEADERS,
                          params={"path": str(target)})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["path"] == str(target)
    assert data["parent"] == str(tmp_path)

    names = [item["name"] for item in data["entries"]]
    # 目录在前、再按名字（不分大小写）：浏览框里先看到能进去的
    assert names == ["sub", "a.txt", "b.txt"]
    for item in data["entries"]:
        assert Path(item["path"]).is_absolute()
        assert (target / item["name"]) == Path(item["path"])
        assert set(item) == {"name", "path", "is_dir"}   # 只列不读，没有别的字段
    assert data["entries"][0]["is_dir"] is True
    assert data["entries"][1]["is_dir"] is False


def test_empty_directory_is_empty_not_error(client, tmp_path):
    target = tmp_path / "void"
    target.mkdir()
    response = client.get("/api/v1/fs", headers=HEADERS,
                          params={"path": str(target)})
    assert response.status_code == 200
    assert response.json()["data"]["entries"] == []


def test_missing_path_is_a_loud_404(client, tmp_path):
    response = client.get("/api/v1/fs", headers=HEADERS,
                          params={"path": str(tmp_path / "nope")})
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "E_DIR_NOT_FOUND"
    assert error["hint"]


def test_file_as_path_is_400_not_a_listing(client, tmp_path):
    """给文件路径不列目录 —— 空列表会被 GUI 读成"空目录"。"""
    target = tmp_path / "subfinder.txt"
    target.write_text("a.example.com\n", encoding="utf-8")
    response = client.get("/api/v1/fs", headers=HEADERS,
                          params={"path": str(target)})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "E_NOT_A_DIR"


def test_parent_of_root_is_none(client):
    """盘符根 / POSIX 根没有上一级 —— parent 为 null，前端据此禁用「上一级」。"""
    roots = client.get("/api/v1/fs", headers=HEADERS).json()["data"]["entries"]
    top = roots[0]["path"]
    data = client.get("/api/v1/fs", headers=HEADERS,
                      params={"path": top}).json()["data"]
    assert data["parent"] is None or data["parent"] == top


def test_browse_then_import_agree_on_the_same_path(client, tmp_path):
    """浏览框里看到的路径，`POST /imports` 必须认 —— 两端不能各说各话。"""
    target = tmp_path / "recon"
    target.mkdir()
    (target / "subfinder.txt").write_text("a.example.com\n", encoding="utf-8")
    seen = client.get("/api/v1/fs", headers=HEADERS,
                      params={"path": str(target)}).json()["data"]["entries"]
    file_path = next(item["path"] for item in seen if not item["is_dir"])
    assert Path(file_path).exists()          # 快速失败判据与这里看到的完全一致
    client.post("/api/v1/projects", headers=HEADERS, json={"name": "demo"})
    response = client.post("/api/v1/imports", headers=HEADERS,
                           json={"paths": [file_path], "project": "demo"})
    assert response.status_code == 202, response.text
