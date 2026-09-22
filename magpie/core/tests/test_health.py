"""3.6 · 六类问题（§7.4 的体检页）的验收网（第六类是 4.1 加的）。

这一页的验收判据只有一句话，而它的两半缺一不可：

    **清空 Inbox 后体检页六类全部归零**
    **六类各自都有能造出非零计数的夹具**

后半句是前半句的**牙齿**。一个"归零"的断言单独放在那里，可能是真的定理，
也可能只是"从来没检测到任何东西"——两者在测试输出里长得一模一样（都是绿色的
一个 0）。所以每类各有一个用例，先把那一类的计数顶起来：

    孤儿        → 导入 → `ops.undo(purge=True)` → `orphan_subject`
    冲突        → 同一实体两个来源报了两个不同的值 → `value_conflict`
    低置信度    → 一份一半是垃圾的清单，嗅探不采用任何解析器 → `manual_required`
    未知字段    → 解析器声明了 known_fields，输入里有别的名字 → `evidence_missing`
    越界资产    → 资产不在项目 scope 的 include 里 → `scope_drift`
    未登记资产  → 一份**会话来源**的导入，带一台此前没登记过的主机（4.1）→
                  `unregistered_asset`

然后 `test_clearing_the_inbox_zeroes_all_five` 把这六个夹具**放进同一个项目**，
先断言六类都非零，再把 Inbox 一条条裁决掉，断言六类全部归零 —— 这才是一条有
牙齿的"清空即干净"。

为什么判据是"Inbox 的归组"而不是五条独立的检测语句：见
`query.HEALTH_CATEGORIES` 上方的长注释。一句话版本是 —— 五条独立的检测语句
在裁决之后**仍然**为真（那个字段还是有两个值），于是"清空即干净"永远兑不了现。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from magpie.core import ingest, ops, query, store
from magpie.core.manual import ManualWriter

#: `inbox_items.kind` CHECK 里的**八个**（001 的七个 + 4.1 在迁移 011 里加的
#: `unregistered_asset`）。抄在测试里是刻意的：**这张表与分类表必须逐字相等**
#: —— 迁移里加了一个 kind 而没人给它归类时，这条用例会红，而不是让那条待办在
#: 体检页上永远不被看见。
SCHEMA_INBOX_KINDS = (
    "value_conflict", "orphan_subject", "identity_ambiguous", "merge_conflict",
    "evidence_missing", "scope_drift", "manual_required", "unregistered_asset",
)

SCOPE = {"include": ["*.example.com"], "exclude": []}


@pytest.fixture()
def conn(tmp_path):
    connection = store.connect(tmp_path / "magpie.db")
    store.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture()
def project(conn):
    return ops.create_project(conn, "health-test", SCOPE)


def options(conn, project_id: int, data_dir: Path) -> ingest.IngestOptions:
    return ingest.IngestOptions(project_id=project_id, data_dir=data_dir, actor="test")


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def httpx_row(host: str, *, webserver: str = "nginx/1.24.0", **extra) -> str:
    row = {"timestamp": "2026-01-01T00:00:00Z", "port": "443",
           "url": f"https://{host}", "input": host, "title": "T", "scheme": "https",
           "webserver": webserver, "content_type": "text/html",
           "status_code": 200, "tech": ["nginx"]}
    row.update(extra)
    return json.dumps(row, ensure_ascii=False)


#: 黄金夹具目录（4.1 起这段夹具要用到 har 的那一份）。
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def counts(conn, project_id: int) -> dict[str, int]:
    report = query.health_report(conn, project_id)
    return {c["key"]: c["count"] for c in report["categories"]}


def resolve_everything(conn, project_id: int) -> int:
    """把 Inbox 一条条**真的裁决掉**（走 `ManualWriter`，与界面同一条路）。"""
    writer = ManualWriter(conn, actor="test")
    items = query.inbox_items(conn, project_id, state="open", limit=query.MAX_PAGE)
    for item in items:
        writer.resolve_inbox(item_id=int(item["id"]), state="resolved")
    return len(items)


# ---------------------------------------------------------------------------
# 分类表本身：七个 kind 必须被**划分**（不漏、不重）
# ---------------------------------------------------------------------------


def test_every_category_has_a_named_query():
    """每一类都在 `NAMED_QUERIES` 里有一行 —— 否则 agent **问不出那一类**。

    `magpie schema --json` 是 agent 认识这个库的全部依据（5.2），而它打出来的
    具名查询清单来自 `query.NAMED_QUERIES`。分类表加一类、清单里漏一行，症状
    不是"少一个便利入口"，而是那一类在 agent 眼里**根本不存在** —— 它不会去问
    一个自己不知道名字的问题，也不会有任何页面因此变红。

    这条断言把"两处词表要一起动"变成一条会响的约束（4.1 加第六类时补的：
    两处**确实**是手写的，而它们之间此前没有任何东西拉着）。
    """
    assert {f"health.{c.key}" for c in query.HEALTH_CATEGORIES} <= set(
        query.NAMED_QUERIES), sorted(set(query.NAMED_QUERIES))


def test_the_five_categories_partition_every_inbox_kind():
    """schema 的八个 kind 恰好被六类分完 —— 没有漏、没有重。

    漏了：那类待办在体检页上永远不会出现（而它明明开着）。
    重了：六类之和大于未决总数，一个项目看起来比它自己还多待办。

    （名字里的 "five" 是 3.6 的历史名字：4.1 加了第六类「未登记资产」之后它是
    `test_the_categories_partition_every_inbox_kind` 更准确 —— 但改名会让
    "哪条用例是 3.6 立的那条"这条线索断掉，而它守的判据一个字都没变。）
    """
    assert sorted(query.HEALTH_KIND_TO_CATEGORY) == sorted(SCHEMA_INBOX_KINDS)
    seen: list[str] = []
    for category in query.HEALTH_CATEGORIES:
        assert category.kinds, f"{category.key} 一个 kind 都没有"
        seen.extend(category.kinds)
    assert sorted(seen) == sorted(SCHEMA_INBOX_KINDS), "同一个 kind 落进了两类"


def test_a_category_is_not_a_second_declaration_of_the_table():
    """`health_category_for` 是从分类表生成的，不是第二份手写映射。"""
    for kind, key in query.HEALTH_KIND_TO_CATEGORY.items():
        assert query.category_spec(key).key == key
        assert kind in query.category_spec(key).kinds
    assert query.health_category_for("no_such_kind") is None


def test_an_unclaimed_kind_is_reported_rather_than_hidden(conn, project):
    """开一条六类都不认领的待办 → `unclaimed` 要**报出来**，而不是静默丢掉。

    `schema` 的 CHECK 挡不住这条（那个 CHECK 是七个 kind 的白名单，而这里要防的
    是"分类表漏了一个"）。一条不被任何一页显示的待办，和没有这条待办，对人来说
    是同一件事 —— 所以它必须让体检页自己说出来。
    """
    ops._open_inbox(conn, project_id=project, kind="identity_ambiguous",  # noqa: SLF001
                    subject_kind="entity", subject_id=0, values=["x"], sources=[])
    report = query.health_report(conn, project)
    assert report["unclaimed"] == []
    assert report["claimed"] == report["open_inbox"] == 1

    # 把分类表临时改窄，模拟"迁移加了一个 kind 而没人归类"
    original = query.HEALTH_CATEGORIES
    try:
        query.HEALTH_CATEGORIES = tuple(
            c for c in original if c.key != "conflict")
        query.HEALTH_KIND_TO_CATEGORY = {
            k: c.key for c in query.HEALTH_CATEGORIES for k in c.kinds}
        report = query.health_report(conn, project)
        assert report["unclaimed"] == [{"kind": "identity_ambiguous", "n": 1}]
        assert any("不会被看见" in note for note in report["notes"])
    finally:
        query.HEALTH_CATEGORIES = original
        query.HEALTH_KIND_TO_CATEGORY = {
            k: c.key for c in original for k in c.kinds}


# ---------------------------------------------------------------------------
# 六类各自的夹具 —— 每一类都要真的有东西能被检测到
# ---------------------------------------------------------------------------


def test_orphan_fixture_makes_the_count_non_zero(conn, project, tmp_path):
    """孤儿：一条**从未有过任何贡献**的缓存残件（§2.3），在 GC 评估时被报出来。

    这类对象不是"被撤销掉的实体" —— 撤销掉的实体带着已抑制的贡献，v3.2-r2 之后
    属于**墓碑**（保留、可解释），`purge_eligible` 会把它列在 `retained` 里。
    真正的孤儿是投影中断之类留下的、**一条贡献都没有过**的行；
    `store.new_entity` 就是这种行的制造方式（`test_invariants` 的孤儿用例同款）。

    §2.3 说它**不得自动清理** —— 所以它进的是 Inbox（`orphan_subject`），不是
    被删掉。生产者是 `ops.undo(purge=True)` 里的 GC 评估那一步。
    """
    store.new_entity(conn, project_id=project, kind="domain",
                     entity_key="leftover.example.com")
    path = write(tmp_path / "d.txt", "app.example.com\n")
    report = ingest.ingest_file(conn, options(conn, project, tmp_path), path)
    assert report["state"] == "done"
    assert counts(conn, project)["orphan"] == 0, "还没做 GC 评估，不该有孤儿"

    ops.undo(conn, report["job_id"], actor="test", purge=True)
    assert counts(conn, project)["orphan"] == 1, "GC 评估没有把孤儿报出来"


def test_conflict_fixture_makes_the_count_non_zero(conn, project, tmp_path):
    """冲突：同一台机器的 `webserver` 被两个文件报成了两个值。

    两个来源都是高置信度 → 投票出 `conflict` → `refresh_observation_conflicts`
    开一条 `value_conflict`。**禁止 last-write-wins 自动选一个值**（§6.3）。
    """
    one = write(tmp_path / "a.jsonl", httpx_row("app.example.com",
                                                webserver="nginx/1.24.0") + "\n")
    two = write(tmp_path / "b.jsonl", httpx_row("app.example.com",
                                                webserver="apache/2.4.58") + "\n")
    ingest.ingest_file(conn, options(conn, project, tmp_path), one)
    ingest.ingest_file(conn, options(conn, project, tmp_path), two)

    report = query.health_report(conn, project)
    conflict = next(c for c in report["categories"] if c["key"] == "conflict")
    assert conflict["count"] > 0, "两个来源报了两个值，却没有冲突待办"
    assert {i["kind"] for i in conflict["items"]} == {"value_conflict"}


def test_low_confidence_fixture_makes_the_count_non_zero(conn, project, tmp_path):
    """低置信度：一份一半是垃圾的清单 —— 嗅探仲裁**不猜**，留一条待办（§5.4）。"""
    path = write(tmp_path / "dirty.txt",
                 "app.example.com\nnot a domain\n- bad\nlocalhost\n"
                 "also bad\napi.example.com\n")
    report = ingest.ingest_file(conn, options(conn, project, tmp_path), path)
    assert report["state"] == "needs_confirmation"
    assert report["counts"]["records"] == 0, "猜错的代价是垃圾灌进资产镜头"

    report = query.health_report(conn, project)
    low = next(c for c in report["categories"] if c["key"] == "low_confidence")
    assert low["count"] == 1
    assert low["items"][0]["kind"] == "manual_required"


def test_unknown_field_fixture_makes_the_count_non_zero(conn, project, tmp_path):
    """未知字段：输入里出现了解析器不认识的键（§5.6 的格式漂移信号）。

    这条以前**只活在导入报告的文件里**（`ctx.unknown_fields` → `<job_id>.json`），
    第二天就查不到了。它现在是一条待办 —— 要人做的裁决是"那个字段是什么"。
    """
    path = write(tmp_path / "new.jsonl",
                 httpx_row("app.example.com", brand_new_field="42") + "\n")
    report = ingest.ingest_file(conn, options(conn, project, tmp_path), path)
    assert report["counts"]["unknown_fields"] == 1

    report = query.health_report(conn, project)
    unknown = next(c for c in report["categories"] if c["key"] == "unknown_field")
    assert unknown["count"] == 1
    item = unknown["items"][0]
    assert item["kind"] == "evidence_missing"
    assert item["payload"]["field"] == "brand_new_field"
    assert item["payload"]["reason"] == "unknown_field"


def test_out_of_scope_fixture_makes_the_count_non_zero(conn, project, tmp_path):
    """越界资产：`*.example.com` 之外的一台机器被导进来了。

    这是 §6 承诺过的"scope 越界告警"（`projects.scope` 从 001 起就在，匹配这一半
    直到 3.6 才有人写）。
    """
    path = write(tmp_path / "d.jsonl", "\n".join([
        httpx_row("app.example.com"),
        httpx_row("evil.other.net"),
    ]) + "\n")
    report = ingest.ingest_file(conn, options(conn, project, tmp_path), path)
    assert report["state"] == "done"

    report = query.health_report(conn, project)
    outside = next(c for c in report["categories"] if c["key"] == "out_of_scope")
    assert outside["count"] == 1, "范围外的那一台没有被报出来"
    item = outside["items"][0]
    assert item["kind"] == "scope_drift"
    assert item["payload"]["entity_key"] == "evil.other.net"
    assert item["payload"]["scope"] == SCOPE


def test_a_project_without_a_scope_reports_nothing(conn, tmp_path):
    """**没有 scope 的项目一条越界都不该有**（`in_scope` 回 None = 不问）。

    这条是反例：把"没配 scope"读成"全越界"，会让每个项目在第一次导入时就刷出
    一屏假待办 —— 那是把体检页变成噪音，而噪音会被忽略。
    """
    free = ops.create_project(conn, "unscoped", {"include": [], "exclude": []})
    path = write(tmp_path / "d.jsonl", httpx_row("whatever.other.net") + "\n")
    ingest.ingest_file(conn, options(conn, free, tmp_path), path)
    assert counts(conn, free)["out_of_scope"] == 0


# ---------------------------------------------------------------------------
# 验收判据：清空 Inbox → 六类全部归零
# ---------------------------------------------------------------------------


def test_clearing_the_inbox_zeroes_all_five(conn, project, tmp_path):
    """**本步的验收判据。**六类先都有东西，再一条条裁决掉，然后必须全部归零。

    先断言"都非零"再断言"都归零"是这条用例的全部价值：只做后一半的话，一个
    从来没检测到任何东西的实现同样能过 —— 那个绿是没有信息的。

    LARI-35 加强了前半句的对偶：**多值字段导入之后 Inbox 必须是干净的**（见下面
    第一段）。没有那一半，"六类都非零 → 归零"完全可能在"假冲突把 Inbox 填满"的
    实现上也成立 —— 裁决掉一堆本就不该存在的东西，归零同样是绿的。

    ⚠️  4.1 加了**第六类**「未登记资产」，所以下面也多了一段夹具（一份会话来源
    的导入）：少了它，那一类永远恒等于 0，而"六类都非零"这条断言会当场红 ——
    那正是它该有的反应（`all(...)` 里混进一个恒零的类别，等于把一半判据悄悄
    关掉）。
    """
    # ---- LARI-35: 多值字段不该进 Inbox（"清空即干净"能不能兑现的前提）----
    # 一份被折过的 endpoint（三条 `path`）+ 一个两条 CNAME 的域名。它们曾经各开
    # 一条 `value_conflict`，于是"清空 Inbox 即干净"这条承诺在导入那一刻就已经
    # 兑不了现了 —— 要靠人一条条裁决掉一堆**本就不该存在**的待办。
    ingest.ingest_file(conn, options(conn, project, tmp_path),
                       write(tmp_path / "folded.txt",
                             "https://app.example.com/user/123\n"
                             "https://app.example.com/user/456\n"))
    ingest.ingest_file(conn, options(conn, project, tmp_path),
                       write(tmp_path / "multi-cname.jsonl",
                             httpx_row("b.example.com",
                                       cname=["x.cdn.net", "y.cdn.net"]) + "\n"))
    assert counts(conn, project) == {c.key: 0 for c in query.HEALTH_CATEGORIES}, \
        "多值字段导入之后 Inbox 就该是干净的（这一步买的正是这个）"

    # 孤儿：一条从未有过贡献的残件 → 一次导入 + 撤销（GC 评估那一步开待办）
    store.new_entity(conn, project_id=project, kind="domain",
                     entity_key="leftover.example.com")
    orphan_job = ingest.ingest_file(
        conn, options(conn, project, tmp_path),
        write(tmp_path / "orphan.txt", "app.example.com\n"))
    ops.undo(conn, orphan_job["job_id"], actor="test", purge=True)

    # 冲突：同一实体两个来源两个值
    ingest.ingest_file(conn, options(conn, project, tmp_path),
                       write(tmp_path / "c1.jsonl", httpx_row("c.example.com",
                                                             webserver="nginx/1.24.0") + "\n"))
    ingest.ingest_file(conn, options(conn, project, tmp_path),
                       write(tmp_path / "c2.jsonl", httpx_row("c.example.com",
                                                             webserver="apache/2.4.58") + "\n"))

    # 低置信度：嗅探拿不准
    ingest.ingest_file(conn, options(conn, project, tmp_path),
                       write(tmp_path / "dirty.txt",
                             "app.example.com\nnot a domain\n- bad\nlocalhost\n"
                             "also bad\napi.example.com\n"))

    # 未知字段 + 越界资产：一份 httpx 输出，带新字段、含范围外的主机
    ingest.ingest_file(conn, options(conn, project, tmp_path),
                       write(tmp_path / "n.jsonl", "\n".join([
                           httpx_row("app.example.com", brand_new_field="42"),
                           httpx_row("evil.other.net"),
                       ]) + "\n"))

    # 未登记资产（4.1）：一份**会话来源**的导入 —— `har` 的黄金夹具里有
    # `api.example.com` 与 `cdn.example.com` 两台主机，而这个项目此前从未登记过
    # 它们。用**真夹具**而不是现造一份：这条路径（`flow --involves--> 主机`）的
    # 判据挂在会话真实产生的关系上，现造一份最小样例很容易造出一个对得上、
    # 而真实夹具对不上的形状。
    ingest.ingest_file(conn, options(conn, project, tmp_path),
                       write(tmp_path / "session.json",
                             (FIXTURES / "har" / "1" / "input.json").read_text(
                                 encoding="utf-8")))

    before = counts(conn, project)
    assert all(n > 0 for n in before.values()), f"夹具没有把六类都顶起来：{before}"

    # ---- 清空 Inbox（走真裁决，不是直接 UPDATE 状态）----
    assert resolve_everything(conn, project) == sum(before.values())

    after = query.health_report(conn, project)
    assert {c["key"]: c["count"] for c in after["categories"]} == {
        c.key: 0 for c in query.HEALTH_CATEGORIES}
    assert after["open_inbox"] == 0
    assert after["claimed"] == 0
    assert after["unclaimed"] == [], "裁决完之后还剩下没人认领的 kind"


def test_clearing_is_about_the_queue_not_about_rewriting_the_facts(conn, project,
                                                                  tmp_path):
    """裁决一条待办**不改事实** —— 它只是"这件事我看过了"。

    这条是上面那条断言的地基：如果"归零"是靠把冲突数据删掉换来的，那它就不是
    "清空即干净"，而是"把体温计摔了"。事实（观测）一条不少。
    """
    path = write(tmp_path / "new.jsonl",
                 httpx_row("evil.other.net", brand_new_field="42") + "\n")
    ingest.ingest_file(conn, options(conn, project, tmp_path), path)

    def facts():
        return tuple(conn.execute(
            "SELECT COUNT(*) FROM observations WHERE project_id=? AND is_current=1",
            (project,)).fetchone())

    before = facts()
    assert resolve_everything(conn, project) == 2      # 未知字段 + 越界资产
    assert facts() == before, "裁决把事实也改掉了"


# ---------------------------------------------------------------------------
# scope 求值本身（越界判据的唯一实现）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pattern,kind,key,expected", [
    # 子域通配：匹配子域，**也收 apex 自己**（理由见 scope.py 的匹配规则）
    ("*.example.com", "domain", "api.example.com", True),
    ("*.example.com", "domain", "example.com", True),
    ("example.com", "domain", "example.com", True),
    ("example.com", "domain", "api.example.com", False),
    # 大小写折叠（且与平台无关）
    ("*.Example.COM", "domain", "API.example.com", True),
    # 网段包含：ip 落在里面 / cidr 是它的子网
    ("10.0.0.0/24", "ip", "10.0.0.7", True),
    ("10.0.0.0/24", "ip", "10.0.1.7", False),
    ("10.0.0.0/24", "cidr", "10.0.0.0/25", True),
    ("10.0.0.0/24", "cidr", "10.0.0.0/24", True),
    ("10.0.0.0/24", "cidr", "10.0.1.0/24", False),
])
def test_scope_matching_rules(pattern, kind, key, expected):
    assert query.scope_module.in_scope({"include": [pattern]}, kind, key) is expected


def test_scope_is_three_valued_so_that_no_scope_never_reads_as_out_of_scope():
    """`None`（不问）与 `False`（越界）必须分得开。

    整个「越界资产」这一类的信噪比都压在这个区分上：把"没配 scope"和"没有地址
    语义的 kind"都变成 `False`，页面上就会出现一批永远清不掉的假待办。
    """
    assert query.scope_module.in_scope({"include": [], "exclude": []},
                                       "domain", "a.example.com") is None
    assert query.scope_module.in_scope({"include": ["*.example.com"]},
                                       "product", "nginx@1.24.0") is None
    assert query.scope_module.in_scope({"include": ["*.example.com"]},
                                       "domain", "a.other.net") is False


def test_exclude_beats_include():
    """黑名单比白名单语气更强：命中 exclude 就是越界，哪怕 include 也命中。"""
    scope = {"include": ["*.example.com"], "exclude": ["secret.example.com"]}
    assert query.scope_module.in_scope(scope, "domain", "app.example.com") is True
    assert query.scope_module.in_scope(scope, "domain", "secret.example.com") is False
