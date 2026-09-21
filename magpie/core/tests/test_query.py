"""3.5 · `core/query.py` 具名查询的验收网（计划书 §3.5）。

**这一步在还一笔明写的债**，所以这里的用例有一条主线：把 §3.5 的五个问题各问
一遍，并且问在**真实夹具**上（nmap + nuclei + dirsearch 三个真文件导入之后）。
一个只在手捏的库上成立的查询，恰好会在真实数据上失败 —— 而真实数据里才有
"同一台机器上 80 与 443 是两个服务""命中同一个目标两次算出同一个 key"这些形状。

另外三件事各有一组断言:

  * **空结果必须是诚实的**：`nginx@9.9` 与 `nginx@1.24.0` 都会回 0 条，但前者
    的意思是"这个产品**还没被写进库**"，后者是"有，但没人跑"。两者的区别只能
    由 `notes` 说出来 —— 少了它，空列表会被读成"筛过了，就是没有"。
  * **包含判断是算出来的**：`in_cidr` 拿地址与网段逐个比对（nmap 的 `<target>`
    不与主机连边），所以它必须**明说**这件事，而不是让结果看起来像一条边。
  * **参数校验与项目解析的先后**：`check_page_filters` 先于 `resolve_project`。
    顺序错了的后果不是报错本身，而是**报错指向了错误的输入**（空库上
    `--limit 0` 报"必须显式给 --project"）。

不在这里的：GUI / CLI 两条路给出同一个答案那一条，属于**跨入口**的断言，
住在 `magpie/api/tests/test_query_convergence.py`（core 的测试不许 import
fastapi —— R6 只给测试目录开了 pytest 的口子）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from magpie.core import ingest, ops, query, store

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "magpie" / "core" / "tests" / "fixtures"
NMAP = FIXTURES / "nmap" / "1" / "input.xml"
NUCLEI = FIXTURES / "nuclei" / "1" / "input.jsonl"
DIRSEARCH = FIXTURES / "dirsearch" / "1" / "input.txt"


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = store.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    store.init_db(conn)
    yield conn
    conn.close()


@pytest.fixture()
def loaded(db: sqlite3.Connection, tmp_path: Path) -> int:
    """真实夹具导入一次，返回 project_id。

    三个文件刻意选这三份：它们一起覆盖了 §3.5 五个问题要问的全部 kind
    （`nmap` → ip / cidr / service / product，`nuclei` → vuln + severity/state
    观测，`dirsearch` → endpoint + `served_by` 那条边）。
    """
    project_id = ops.create_project(
        db, "query", {"include": ["*.example.com"], "exclude": []})
    options = ingest.IngestOptions(project_id=project_id,
                                   data_dir=tmp_path / "data", archive="external")
    report = ingest.ingest_paths(db, options, [NMAP, NUCLEI, DIRSEARCH])
    assert all(f["state"] in ("done", "partial") for f in report["files"]), report
    return project_id


# ---------------------------------------------------------------------------
# §3.5 的五个问题
# ---------------------------------------------------------------------------


def test_who_still_runs_nginx_1_24_0(db, loaded):
    """「哪些还在用 Nginx 1.24.0」——一问一答，不经过 kind 的中间步骤。

    答案里是 **service**（"谁在跑"问的是服务），不是 product。带版本与不带版本
    是两个实体这件事在这一条上兑现：`nginx` 与 `nginx@1.24.0` 各问一次，得到
    的答案不同（后者才是"这个版本"）。
    """
    result = query.services_running(db, loaded, "Nginx", version="1.24.0")
    assert result["query"] == "services"
    assert result["filters"]["product"] == "nginx@1.24.0"      # 别名折叠在这里发生
    keys = sorted(e["entity_key"] for e in result["entities"])
    assert keys == ["203.0.113.10:443", "203.0.113.10:80"]
    assert {e["kind"] for e in result["entities"]} == {"service"}
    assert result["notes"] == []

    # 不带版本问的是另一个实体：`nginx` 这一条在夹具里不存在 → 0 条 + 一条
    # **说明为什么**的 note。
    bare = query.services_running(db, loaded, "nginx")
    assert bare["count"] == 0
    assert any("nginx" in note for note in bare["notes"]), bare["notes"]


def test_a_missing_product_says_it_is_missing_not_unused(db, loaded):
    """空结果必须带着"为什么是空的"。

    这一条与上一条是一对：`nginx@9.9` 与 `nginx@1.24.0` 之外的任何版本都会回
    0 条，但只有 note 能区分"这个产品还没被写进库"与"有，但没人跑它"。
    """
    result = query.services_running(db, loaded, "nginx@9.9.9")
    assert result["count"] == 0
    assert result["entities"] == []
    assert len(result["notes"]) == 1
    assert "nginx@9.9.9" in result["notes"][0]
    assert "还没被任何来源写进来" in result["notes"][0]


def test_lens_kind_counts_are_facts_not_declarations(db, loaded):
    """镜头页的类型徽标：计数来自**这个项目当前的实体**，不是登记表里的声明。

    `assets` 镜头下有 kind 却没有实体时计数是 0 —— 一个"登记过但一条都没有"
    的 kind 不在这里被抹掉（那会让界面少一行），也不被算成 1。
    """
    view = query.lens_view(db, loaded, "services")
    assert {k["kind"] for k in view["kinds"]} == {"product", "service"}
    assert all(k["count"] > 0 for k in view["kinds"])
    assert view["kinds"][0]["identity_rule"], "镜头页要能显示身份规则"

    counts = query.kind_counts(db, loaded)
    assert counts["service"] >= 2 and counts["cidr"] == 1
    # 计数里的每一个 kind 都必须是**登记过**的 —— 未登记的 kind 写不进库
    # （003 的拒绝触发器），所以这条断言两边都在守同一件事。
    assert set(counts) <= {k["kind"] for k in query.kinds(db)}


def test_vuln_filters_go_through_observations(db, loaded):
    """按 `severity` / `state` 筛漏洞。

    两者都是**观测**而不是列（005 的迁移注释：它们是事实，由谁在哪次作业里
    说的），所以这里同时钉住一件事：返回的实体行里**没有** `severity` 字段 ——
    有的话就意味着有人把它升格成了一列，而那条路会绕开 contributions 模型。
    """
    critical = query.vulns(db, loaded, severity="critical")
    assert critical["count"] >= 1
    assert all(e["kind"] == "vuln" for e in critical["entities"])
    assert all("severity" not in e for e in critical["entities"])

    both = query.vulns(db, loaded, severity="critical", state="open")
    assert both["count"] == critical["count"], "夹具里 severity 与 state 是正交的"

    none = query.vulns(db, loaded, severity="nonexistent")
    assert none["count"] == 0 and none["notes"] == [], \
        "severity 筛不中不是边界情况，是纯粹的没有 —— 这里不该有 note"


def test_endpoint_owner_is_the_service_behind_it(db, loaded):
    """「这个接口归谁」问的是**服务**（`endpoint --served_by--> service`）。

    身份含主机，所以三种写法各自成立：绝对 URL（顺带做路径模板折叠）、
    折叠好的规范 key、以及**裸路径要响亮地拒绝**（替人挑一个 base 就是编事实）。
    """
    by_url = query.endpoint_owners(db, loaded, "http://app.example.com:8080/user/123")
    assert by_url["filters"]["endpoint"] == "app.example.com:8080/user/{id}"
    assert [e["entity_key"] for e in by_url["entities"]] == ["app.example.com:8080"]

    by_key = query.endpoint_owners(db, loaded, "app.example.com:8080/user/{id}")
    assert [e["entity_key"] for e in by_key["entities"]] == ["app.example.com:8080"]

    with pytest.raises(query.QueryError) as excinfo:
        query.endpoint_owners(db, loaded, "/user/123")
    assert excinfo.value.code == "E_BAD_ENDPOINT"
    assert "主机" in excinfo.value.message


def test_assets_in_cidr_is_a_computed_containment(db, loaded):
    """「哪些资产落在网段里」——**算出来的**，而且这件事写在 `notes` 里。

    三件事一起钉：
      * 主机位被补齐（`203.0.113.7/24` → `203.0.113.0/24`，同一网段的两种写法
        是同一个）后，范围内的三个 ip 都在；
      * 网段**自己**不在自己的结果里（把自己列出来是一条废话）；
      * 结果是包含判断的产物，`notes` 必须说出来 —— 它看起来太像一条存着的边了。
    """
    result = query.assets_in_cidr(db, loaded, "203.0.113.7/24")
    assert result["filters"]["in_cidr"] == "203.0.113.0/24"
    assert sorted(e["entity_key"] for e in result["entities"]) == [
        "203.0.113.10", "203.0.113.11", "203.0.113.20"]
    assert result["notes"], "包含判断必须说明自己是算出来的"

    # 单个 IP 不是网段：`try_cidr` 认不出它 → 响亮地拒绝，而不是当成 /32 猜一个。
    with pytest.raises(query.QueryError) as excinfo:
        query.assets_in_cidr(db, loaded, "203.0.113.10")
    assert excinfo.value.code == "E_BAD_CIDR"


# ---------------------------------------------------------------------------
# 分页 / 过滤 / 校验 —— CLI 与 API 共用的那几条语义
# ---------------------------------------------------------------------------


def test_cursor_paging_is_stable_and_does_not_repeat(db, loaded):
    first = query.entities_page(db, loaded, kind="service", limit=2)
    assert first["count"] == 2 and first["next_cursor"]
    second = query.entities_page(db, loaded, kind="service", limit=2,
                                 cursor=first["next_cursor"])
    ids_first = {e["id"] for e in first["entities"]}
    ids_second = {e["id"] for e in second["entities"]}
    assert not (ids_first & ids_second), "翻页返回了重复的实体"

    # 最后一页没有 next_cursor —— 一个"永远还有下一页"的游标会让调用方
    # 一直翻下去，而每一页都是空的。
    tail = query.entities_page(db, loaded, kind="service", limit=500)
    assert tail["next_cursor"] is None


def test_dispatch_names_the_question_it_answered(db, loaded):
    """`dispatch` 是唯一的分发点：这组过滤条件在问哪一问，只有一处知道。

    CLI 与 API 都从这里进 —— 所以"两个入口会不会选出不同的查询"这个问题
    不存在。名字跟着结果一起回，调用方据此把它写进输出。
    """
    assert query.dispatch(db, loaded, kind="service")["query"] == "entities"
    assert query.dispatch(db, loaded, product="nginx@1.24.0")["query"] == "services"
    assert query.dispatch(db, loaded, severity="critical")["query"] == "vulns"
    assert query.dispatch(db, loaded, endpoint="app.example.com:8080/admin")["query"] \
        == "endpoint"
    assert query.dispatch(db, loaded, in_cidr="203.0.113.0/24")["query"] \
        == "assets_in_cidr"


def test_unsupported_filters_are_rejected_loudly():
    """静默忽略一个过滤条件 = 给调用方一个假答案。**必须响亮地拒绝。**

    `supported` 必须跟着错误一起回去：只说"不支持 review_status"而不说支持
    什么，调用方下一步只能去读源码。
    """
    with pytest.raises(query.QueryError) as excinfo:
        query.reject_unsupported({"review_status": "confirmed", "in_scope": "true"})
    error = excinfo.value
    assert error.code == "E_UNSUPPORTED_FILTER"
    assert error.details["rejected"] == ["in_scope", "review_status"]
    assert "kind" in error.details["supported"]
    assert "review_status" not in error.details["supported"]

    query.reject_unsupported({"review_status": None})         # 空值不算"传了"


def test_bad_limit_and_cursor_are_usage_errors_not_data_errors():
    """`check_page_filters` 的意义是**顺序**：用法错先于数据错。

    这里钉的是它自己会响：一个写错的 `limit` / `cursor` 不落到查询上，而是在
    解析项目之前就被拦下 —— 否则空库上的 `--limit 0` 会报"必须显式给 project"，
    把调用方引向一个与他输入无关的方向。
    """
    with pytest.raises(query.QueryError) as excinfo:
        query.check_page_filters(limit=0)
    assert excinfo.value.code == "E_BAD_LIMIT"

    with pytest.raises(query.QueryError) as excinfo:
        query.check_page_filters(cursor="上一页最后那条")
    assert excinfo.value.code == "E_BAD_CURSOR"

    query.check_page_filters(limit=99999, cursor=None)        # 上限是**截断**不是报错
    assert query.page_size(99999) == query.MAX_PAGE


def test_page_size_caps_instead_of_failing():
    """超过上限是**截断**（回到 MAX_PAGE），不是报错。

    两种做法都说得通，但只有一种能同时满足"界面要 500 条"与"不许拉爆内存"：
    报错会让每一次超限请求都变成一次往返，而调用方只是想多要一点。
    """
    assert query.page_size(10) == 10
    assert query.page_size(query.MAX_PAGE + 1) == query.MAX_PAGE


# ---------------------------------------------------------------------------
# 身份规则：用户写的字面量 → 库里的规范 key
# ---------------------------------------------------------------------------


def test_product_spec_delegates_to_the_identity_rule():
    """产品键由**身份规则的唯一实现**算出（`normalize.product_name` +
    `httpx.product_key` + `normalize.version_tuple`），本模块只负责把用户可能
    写的形状切开。别名折叠与"带版本/不带版本是两个实体"都在这里体现。
    """
    assert query.product_spec("nginx@1.24.0") == "nginx@1.24.0"
    assert query.product_spec("Nginx", "1.24.0") == "nginx@1.24.0"
    assert query.product_spec("Apache httpd") == "httpd"       # 别名折叠
    assert query.product_spec("nginx") == "nginx"

    with pytest.raises(query.QueryError) as excinfo:
        query.product_spec("nginx", "cloudfront")              # 不是版本号
    assert excinfo.value.code == "E_BAD_PRODUCT"

    with pytest.raises(query.QueryError):
        query.services_running(None, 0, None, version="1.24.0")  # 只给版本


def test_endpoint_spec_accepts_a_url_or_a_canonical_key():
    assert query.endpoint_spec("https://app.example.com/user/8f3a2b1c-1111-2222-3333-444455556666") \
        == "app.example.com/user/{uuid}"
    assert query.endpoint_spec("app.example.com/user/{id}") == "app.example.com/user/{id}"

    with pytest.raises(query.QueryError) as excinfo:
        query.endpoint_spec("/user/{id}")
    assert excinfo.value.code == "E_BAD_ENDPOINT"
