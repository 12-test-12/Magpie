"""2.4 · 解析器框架与两个解析器的验收回归网（开发实施计划 2.4）。

**每一条验收标准对应一个用例**，标准写在哪、用例就在哪：

    | 黄金夹具   | 两个解析器各自逐条比对 expected.json，**逐字段**而非只看条数     |
    | 负向夹具   | 每一条都**不崩**，且产出可读的坏行报告（不是 Python 堆栈）        |
    | 未知字段   | 喂一条含新字段的行 → 报告出现「检测到新字段 x（N 行）」           |
    | 词表       | 未登记 kind 被拒（反例红→绿）；每个登记的 kind 三件套齐全         |
    | 硬线       | 解析器只产出 (kind, key)，绝不产出数据库自增 id                   |
    | definition_hash | 缺失即报错（反例红→绿）                                     |
    | 发现       | `magpie.core.parsers` 一落地，`parsers_bridge` 自动退役           |

关于**黄金夹具是生成的**这件事
--------------------------------
`expected.json` 由实现**一次性生成、然后逐条人工读过**（生成脚本随该轮评审一起
看过），此后它就是冻结合同：它不再是"代码现在输出什么"，而是"**当时被确认过的
输出**"。所以它的价值在回归——任何后续改动只要动了任何一个字段，这里就会红，
而报错信息直接指出是第几条记录的第几个字段。

生成脚本没有留在仓库里，是刻意的：留着它就有"重跑一遍让夹具跟上实现"的捷径，
那条捷径恰好把这个文件变成同义反复。
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from magpie.core import ingest, normalize, ops, store
from magpie.core.parsers import base, domainlist, httpx

FIXTURES = Path(__file__).resolve().parent / "fixtures"
KINDS = FIXTURES / "kinds"
REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = store.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    store.init_db(conn)
    yield conn
    conn.close()


_SEQ = [0]


@pytest.fixture()
def project(db) -> int:
    _SEQ[0] += 1
    return ops.create_project(db, f"parser-test-{_SEQ[0]}", {"include": [], "exclude": []})


def options(db, project_id: int, data_dir: Path) -> ingest.IngestOptions:
    return ingest.IngestOptions(project_id=project_id, data_dir=data_dir,
                                actor="test", verify=True)


def encode(rec) -> dict:
    """`Raw*` → 与 `expected.json` 同构的 dict。两边共用同一套字段名。"""
    if isinstance(rec, base.RawObservation):
        return {"type": "observation", "entity_ref": list(rec.entity_ref),
                "field": rec.type, "value": rec.value}
    if isinstance(rec, base.RawRelation):
        return {"type": "relation", "src": list(rec.src), "dst": list(rec.dst),
                "rel": rec.rel}
    out = {"type": "entity", "kind": rec.kind, "key": rec.key, "display": rec.display}
    if rec.subtitle is not None:
        out["subtitle"] = rec.subtitle
    if rec.attrs:
        out["attrs"] = rec.attrs
    return out


def load_case(tool: str, version: str = "1") -> tuple[Path, dict, list]:
    case_dir = FIXTURES / tool / version
    expected = json.loads((case_dir / "expected.json").read_text(encoding="utf-8"))
    text = (case_dir / expected["input"]).read_text(encoding="utf-8")
    return case_dir, expected, text.splitlines()


def parse_with(cls, lines) -> tuple[list, base.OfflineContext]:
    ctx = base.OfflineContext()
    return list(cls().parse(lines, ctx)), ctx


def fixtures_for_kind(kind: str) -> list[Path]:
    """所有 `expected.json` 里出现过该 kind 实体的黄金夹具目录。"""
    found = []
    for path in sorted(FIXTURES.glob("*/*/expected.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if any(r.get("type") == "entity" and r.get("kind") == kind
               for r in doc["records"]):
            found.append(path.parent)
    return found


# ---------------------------------------------------------------------------
# 黄金夹具 —— 逐字段比对（不是只看条数）
# ---------------------------------------------------------------------------

GOLDEN = [("domainlist", domainlist.DomainListParser),
          ("httpx", httpx.HttpxParser)]


@pytest.mark.parametrize("tool,cls", GOLDEN)
def test_golden_fixture_matches_field_by_field(tool, cls):
    """逐条、逐字段比对。条数相同但字段不同同样算失败。"""
    case_dir, expected, lines = load_case(tool, cls.format_version)
    records, ctx = parse_with(cls, lines)
    got = [encode(r) for r in records]

    assert len(got) == len(expected["records"]), (
        f"{case_dir.name}: 记录条数 {len(got)} != {len(expected['records'])}")
    for index, (actual, wanted) in enumerate(zip(got, expected["records"])):
        assert actual == wanted, (
            f"{case_dir.name} 第 {index} 条记录不一致：\n"
            f"  实际 {json.dumps(actual, ensure_ascii=False)}\n"
            f"  期望 {json.dumps(wanted, ensure_ascii=False)}")


@pytest.mark.parametrize("tool,cls", GOLDEN)
def test_golden_fixture_bad_lines_and_unknown_fields(tool, cls):
    case_dir, expected, lines = load_case(tool, cls.format_version)
    _, ctx = parse_with(cls, lines)
    got = [{"line": b["line"], "reason": b["reason"]} for b in ctx.bad_lines]
    assert got == expected["bad_lines"], case_dir.name
    assert ctx.unknown_fields == expected["unknown_fields"], case_dir.name


@pytest.mark.parametrize("tool,cls", GOLDEN)
def test_golden_fixture_claims_a_consistent_identity(tool, cls):
    """`expected.json` 自己必须自洽：每条观测/关系的端点，都要在同文件里被声明过。

    没有它，夹具可以"看起来很详细"，而里面引用的实体**根本没人建** ——
    那种夹具会把一个断链的实现永久固定下来。
    """
    _, expected, _ = load_case(tool, cls.format_version)
    declared = {(r["kind"], r["key"]) for r in expected["records"]
                if r["type"] == "entity"}
    for rec in expected["records"]:
        for ref in ([rec["entity_ref"]] if rec["type"] == "observation"
                    else ([rec["src"], rec["dst"]] if rec["type"] == "relation" else [])):
            assert tuple(ref) in declared, f"{tool}: 引用了未声明的实体 {ref}"


@pytest.mark.parametrize("tool,cls", GOLDEN)
def test_parsers_emit_only_kind_key_references(tool, cls):
    """硬线：解析器只产出 `(kind, key)`，**绝不产出数据库自增 id**。

    判据是"引用里出现了 int" —— 自增 id 一定以 int 形态出现，而 key 永远
    是字符串。这条断言比"看代码里有没有 SELECT"可靠得多。
    """
    _, _, lines = load_case(tool, cls.format_version)
    records, _ = parse_with(cls, lines)
    refs: list = []
    for rec in records:
        if isinstance(rec, base.RawEntity):
            refs.append((rec.kind, rec.key))
        elif isinstance(rec, base.RawObservation):
            refs.append(rec.entity_ref)
        else:
            refs += [rec.src, rec.dst]
    assert refs
    for kind, key in refs:
        assert isinstance(kind, str) and isinstance(key, str), (kind, key)
        assert not key.isdigit(), f"{key!r} 看起来像自增 id"


def test_the_hard_line_is_structural_not_a_convention():
    """硬线由 `__post_init__` 拦下 —— 构造时就炸，不是等写库才炸。"""
    with pytest.raises(TypeError):
        base.RawObservation(entity_ref=(1, "x"), type="t", value="v")
    with pytest.raises(TypeError):
        base.RawRelation(src=("domain", "a"), dst=("domain", 2), rel="member_of")
    with pytest.raises(TypeError):
        base.RawEntity(kind="domain", key=7)


# ---------------------------------------------------------------------------
# 负向夹具 —— 10 条，每一条都不崩、都产出可读报告
# ---------------------------------------------------------------------------

_HUGE = 2 * 1024 * 1024


def _huge_body_line() -> bytes:
    """一条 `body` 字段有 4 MB 的记录（HAR / httpx `-json` 都有这个字段）。"""
    payload = {"timestamp": "2026-09-17T10:00:03.000Z", "host": "big.example.com",
               "input": "big.example.com", "port": "443", "scheme": "https",
               "status_code": 200, "title": "Big", "body": "x" * _HUGE}
    return json.dumps(payload).encode("utf-8")


NEGATIVE = [
    ("empty-file", b"", "空文件"),
    ("bom-only", b"\xef\xbb\xbf", "只有 BOM"),
    ("gbk", "app.example.com\n中文.example.com\n".encode("gbk"), "GBK 编码"),
    ("crlf-and-lone-cr", b"app.example.com\r\napi.example.com\rdev.example.com\n",
     "CRLF + 孤立 CR"),
    ("huge-line", b"a" * _HUGE + b"\n", "超长行"),
    ("nul-bytes", b"app.example.com\x00\napi.example.com\x00\x00\n", "NUL 字节"),
    ("zip", b"PK\x03\x04" + b"\x00" * 64 + b"payload", "zip 压缩包"),
    ("corrupt-json", b'{"host": "app.example.com", "port": "443", "scheme": "https"}\n'
                     b'{"host": "api.example.com", "tech": [}\n', "损坏 JSON"),
]


@pytest.mark.parametrize("name,payload,label", NEGATIVE, ids=[c[0] for c in NEGATIVE])
def test_negative_fixture_never_crashes(db, project, tmp_path, name, payload, label):
    """每一条负向输入都必须走"可读报告"这条路，而不是抛 Python 堆栈。

    判据有三条：`ingest_file` 正常返回；状态是已知的那几个之一；整份报告里
    没有 `Traceback`。第三条看着笨，但它正是"不崩"这句话唯一能被机器检验的形式。
    """
    path = tmp_path / f"{name}.txt"
    path.write_bytes(payload)
    report = ingest.ingest_file(db, options(db, project, tmp_path), path)

    assert isinstance(report, dict), label
    assert report["state"] in {"done", "partial", "skipped", "failed",
                              "needs_confirmation", "cancelled"}, label
    dumped = json.dumps(report, ensure_ascii=False)
    assert "Traceback" not in dumped, f"{label} 漏出了 Python 堆栈"
    assert report.get("reason") or report.get("skip_reason") or report["state"] == "done" \
        or report["counts"]["bad_lines"] or report["counts"]["records"], (
            f"{label}：既没成功也没给理由")


def test_negative_huge_line_is_reported_readably_and_bounded(db, project, tmp_path):
    """超长行：进坏行报告，且**报告里的原文被截断** —— 2 MB 的坏行不该把报告也撑成 2 MB。

    前面刻意垫 10 条正常域名：一份**整体不是**域名清单的文件在嗅探阶段就被拦下了
    （§5.4 的仲裁不采用），根本走不到"逐行解析"这一步 —— 那样就测不到超长行了。
    """
    path = tmp_path / "huge.txt"
    good = b"".join(f"host{i:02d}.example.com\n".encode() for i in range(10))
    path.write_bytes(good + b"a" * _HUGE + b"\n")
    report = ingest.ingest_file(db, options(db, project, tmp_path), path)

    assert report["counts"]["bad_lines"] == 1
    assert report["badlines_report"], "坏行必须落盘"
    text = Path(report["badlines_report"]).read_text(encoding="utf-8")
    assert "不是合法域名" in text
    assert len(text) < 4096, f"坏行报告没有截断原文（{len(text)} 字节）"


def test_negative_huge_body_is_never_stored(db, project, tmp_path):
    """HAR / httpx `-json` 里的超大 `body`：解析不崩，但**正文一个字都不落库**。

    这一条锁的是"正文不入库"这个性质（计划书 §5.3：原始材料只归档、不在主界面
    展示）。真正的 HAR 解析是阶段 4 的事，但这条性质今天就成立、今天就该被守。
    """
    path = tmp_path / "huge-body.jsonl"
    path.write_bytes(
        b'{"host": "small.example.com", "port": "443", "scheme": "https",'
        b' "status_code": 200}\n' + _huge_body_line() + b"\n")
    report = ingest.ingest_file(db, options(db, project, tmp_path), path)

    assert report["state"] in {"done", "partial"}
    assert report["counts"]["bad_lines"] == 0, report["counts"]
    rows = list(db.execute("SELECT semantic_field, value FROM observations"
                           " WHERE entity_id IN (SELECT id FROM entities WHERE id IN"
                           " (SELECT subject_id FROM contributions"
                           "   WHERE subject_kind='observation'))"))
    assert rows
    for field, value in rows:
        assert len(str(value)) < 4096, f"{field} 把正文存进来了（{len(value)} 字节）"


def test_negative_same_name_different_content_is_not_a_duplicate(db, project,
                                                                tmp_path):
    """同名不同内容**不是**同一份材料 —— 身份是 sha256，不是文件名。

    反过来的那一半同样重要：内容相同的两个不同文件名**是**同一份材料，
    第二次导入必须 skipped（2.5 的幂等标准，这里守它的上界）。
    """
    first = tmp_path / "a" / "recon.txt"
    second = tmp_path / "b" / "recon.txt"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text("app.example.com\n", encoding="utf-8")
    second.write_text("other.example.com\n", encoding="utf-8")

    opts = options(db, project, tmp_path)
    a = ingest.ingest_file(db, opts, first)
    b = ingest.ingest_file(db, opts, second)
    assert a["state"] == "done" and b["state"] == "done", (a["state"], b["state"])
    assert a["sha256"] != b["sha256"]

    # 第三份：内容与第一份相同但换了名字 → 必须 skipped（sha256 说了算）
    clone = tmp_path / "c" / "renamed.txt"
    clone.parent.mkdir(parents=True)
    clone.write_text("app.example.com\n", encoding="utf-8")
    again = ingest.ingest_file(db, opts, clone)
    assert again["state"] == "skipped"
    assert again["job_id"] == a["job_id"]


def test_negative_zip_is_archived_as_binary_with_a_readable_reason(db, project,
                                                                  tmp_path):
    """zip 走"归档但不解析"的可读路径 —— 不抛，也不假装读懂了它。"""
    path = tmp_path / "bundle.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("recon.txt", "app.example.com\n")
    report = ingest.ingest_file(db, options(db, project, tmp_path), path)

    assert report["state"] == "skipped"
    assert "zip" in report["skip_reason"]
    assert report["encoding"]["binary"] == "zip"
    assert report["counts"]["records"] == 0


def test_negative_gbk_is_decoded_not_mangled(db, project, tmp_path):
    """GBK 文件按 gb18030 解码（GBK 的超集）—— 中文域名不显示成 \\ufffd。"""
    path = tmp_path / "gbk.txt"
    path.write_bytes("app.example.com\n中文.example.com\n".encode("gbk"))
    report = ingest.ingest_file(db, options(db, project, tmp_path), path)

    assert report["encoding"]["encoding"] == "gb18030"
    assert report["counts"]["bad_lines"] == 0, report["counts"]
    keys = [r[0] for r in db.execute(
        "SELECT ek.entity_key FROM entity_keys ek JOIN entities e ON e.id = ek.entity_id"
        " WHERE e.project_id=? AND e.kind='domain'", (project,))]
    assert "app.example.com" in keys
    assert not any("�" in k for k in keys), keys


def test_negative_crlf_and_lone_cr_are_both_line_breaks(db, project, tmp_path):
    """CRLF / 孤立 CR / LF 都切成逻辑行 —— 孤立 CR 不需要额外分支（newline=None）。"""
    path = tmp_path / "mixed.txt"
    path.write_bytes(b"a.example.com\r\nb.example.com\rc.example.com\n")
    report = ingest.ingest_file(db, options(db, project, tmp_path), path)

    assert report["counts"]["bad_lines"] == 0
    keys = {r[0] for r in db.execute(
        "SELECT ek.entity_key FROM entity_keys ek JOIN entities e ON e.id = ek.entity_id"
        " WHERE e.project_id=? AND e.kind='domain'", (project,))}
    assert {"a.example.com", "b.example.com", "c.example.com"} <= keys


def test_negative_nul_bytes_are_stripped_and_reported(db, project, tmp_path):
    """NUL 在解析前剥离（行号不变），但**报告里说清剥了几个** —— 静默剥离也是撒谎。"""
    path = tmp_path / "nul.txt"
    path.write_bytes(b"app.example.com\x00\napi.example.com\x00\x00\n")
    report = ingest.ingest_file(db, options(db, project, tmp_path), path)

    assert report["encoding"]["nul_bytes"] == 3
    assert any("NUL" in note for note in report["encoding"]["notes"])
    assert report["counts"]["bad_lines"] == 0


# ---------------------------------------------------------------------------
# 未知字段探测（§5.6）
# ---------------------------------------------------------------------------


def test_unknown_field_is_reported_with_the_wording_the_plan_requires():
    """「检测到新字段 `x`（N 行）」——这句话是"先能发现漂移"的唯一可见形态。"""
    lines = [
        json.dumps({"host": "a.example.com", "port": "443", "scheme": "https",
                    "cname_chain": "x,y", "brand_new_field": 1}),
        json.dumps({"host": "b.example.com", "port": "443", "scheme": "https",
                    "cname_chain": "z"}),
    ]
    _, ctx = parse_with(httpx.HttpxParser, lines)

    report = ctx.unknown_field_report()
    assert any("检测到新字段 `cname_chain`（2 行）" in line for line in report), report
    assert any("检测到新字段 `brand_new_field`（1 行）" in line for line in report), report
    # 未知字段**不影响**正常记录：漂移要被发现，但不能让整份文件失败。
    assert ctx.bad_lines == []


def test_unknown_field_reaches_the_ingest_report(db, project, tmp_path):
    path = tmp_path / "drift.jsonl"
    path.write_text(
        '{"host": "app.example.com", "port": "443", "scheme": "https",'
        ' "status_code": 200, "cname_chain": "a,b"}\n', encoding="utf-8")
    report = ingest.ingest_file(db, options(db, project, tmp_path), path)

    assert report["counts"]["unknown_fields"] == 1
    assert report["unknown_fields_detail"] == {"cname_chain": 1}


# ---------------------------------------------------------------------------
# 词表（同期必交项 A）
# ---------------------------------------------------------------------------


def test_unregistered_kind_is_rejected_by_the_database(db, project):
    """反例红→绿：未登记的 kind **必然被拒**（不是靠自觉）。"""
    with pytest.raises(sqlite3.IntegrityError) as exc:
        store.new_entity(db, project_id=project, kind="endpoint", entity_key="x")
    assert "未登记的 kind" in str(exc.value)


def test_unregistered_relation_verb_is_rejected_by_the_database(db, project):
    """关系动词与 kind 一样：未登记就写不进去。"""
    src = store.new_entity(db, project_id=project, kind="domain",
                           entity_key="a.example.com")
    with pytest.raises(sqlite3.IntegrityError) as exc:
        db.execute("INSERT INTO relations(project_id, src_id, dst_id, rel, created_at)"
                   " VALUES (?,?,?,'exposes','now')", (project, src, src))
    assert "未登记的关系动词" in str(exc.value)
    # 正例：登记过的动词照常写得进去（反例红、正例绿，缺一不可）
    db.execute("INSERT INTO relations(project_id, src_id, dst_id, rel, created_at)"
               " VALUES (?,?,?,'resolves_to','now')", (project, src, src))


def test_kind_registry_matches_the_python_mirror(db):
    """schema 里的词表与 `base.py` 的镜像是**同一份** —— 分叉立刻红。

    为什么要两条落点：schema 是唯一事实来源（它守得住数据层），Python 镜像让
    解析器能在产出记录时给出可读的早期报错。两份一旦不同步，"库里能写的
    kind 解析器不认"这种最难查的问题就出现了，所以这里必须断言一致。
    """
    rows = {r["kind"]: r for r in db.execute("SELECT * FROM kind_registry")}
    assert set(rows) == set(base.KIND_BY_NAME), (set(rows), set(base.KIND_BY_NAME))
    for kind, spec in base.KIND_BY_NAME.items():
        assert rows[kind]["lens"] == spec.lens, kind
        assert rows[kind]["fixture_path"] == spec.fixture, kind
        assert json.loads(rows[kind]["produced_by"]) == list(spec.produced_by), kind

    verbs = {r["verb"]: r for r in db.execute("SELECT * FROM relation_vocabulary")}
    assert set(verbs) == set(base.RELATION_BY_NAME)
    for verb, spec in base.RELATION_BY_NAME.items():
        assert verbs[verb]["description"] == spec.description, verb
        assert verbs[verb]["inverse"] == (spec.inverse or None), verb


def test_every_registered_kind_has_all_three_parts(db):
    """三件套齐全（v3.2 §12）：身份规则 + 样例夹具 + 镜头归属，缺一不可。

    而且"样例夹具"必须是**可执行的证据**：夹具里的每条样例都要被声明的规则
    真的算成声明的 key。一句描述的夹具不算夹具。
    """
    rules = _identity_rules()
    for row in db.execute("SELECT * FROM kind_registry ORDER BY kind"):
        kind = row["kind"]
        assert row["identity_rule"].strip(), f"{kind}: 缺身份规则"
        assert row["lens"] in base.LENSES, f"{kind}: 镜头归属不在 §3.5 的五个镜头里"

        fixture = FIXTURES.parent / row["fixture_path"]
        assert fixture.is_file(), f"{kind}: 夹具不存在 {row['fixture_path']}"
        doc = json.loads(fixture.read_text(encoding="utf-8"))
        assert doc["kind"] == kind
        # 身份规则的正文有两份落点（登记表 + 夹具），它们**必须逐字一致** ——
        # 两份描述一旦分叉，"哪个才是身份规则"就没有答案了。
        assert doc["identity_rule"] == row["identity_rule"], f"{kind}: 身份规则两处不一致"
        assert doc["lens"] == row["lens"]
        assert doc["examples"], f"{kind}: 夹具里没有样例"

        for example in doc["examples"]:
            rule = rules[example["rule"]]
            assert rule(*example["args"]) == example["key"], (
                f"{kind}: 身份规则算不出声明的 key —— {example}")
        for reject in doc.get("rejects", []):
            _assert_rejected(rules[reject["rule"]], reject)


def test_every_registered_kind_has_a_real_producer(db):
    """`produced_by` 不是一句声明：夹具里必须真的出现这个 kind 的实体。

    没有这条断言，`produced_by: ["httpx@1"]` 可以是一句谁也验不了的注释。
    """
    for row in db.execute("SELECT kind, produced_by FROM kind_registry"):
        declared = json.loads(row["produced_by"])
        if not declared:
            continue                    # 名称级兜底 kind（host）没有产出者，是登记在案的
        found = fixtures_for_kind(row["kind"])
        assert found, f"{row['kind']}: 声明了产出者 {declared}，但没有任何夹具里出现它"


def test_unregistered_kind_gets_a_readable_python_error_too():
    """Python 镜像也拒 —— 而且报错说的是"缺哪一件套"，不是一句约束失败。"""
    with pytest.raises(base.VocabularyError) as exc:
        base.require_kind("apischema")
    assert "身份规则 + 样例夹具 + 镜头归属" in str(exc.value)
    with pytest.raises(base.VocabularyError):
        base.require_relation("exposes")


def _identity_rules() -> dict:
    """夹具里可以引用的身份规则（白名单）。

    白名单而不是 `eval`：夹具是数据，数据不该有执行任意表达式的权力。
    """
    def literal(*args):
        value = args[0]
        if not isinstance(value, str) or not value:
            return None
        return value

    return {
        "normalize.normalize_domain": normalize.normalize_domain,
        "normalize.normalize_ip": normalize.normalize_ip,
        "normalize.normalize_cidr": normalize.normalize_cidr,
        "normalize.product_name": normalize.product_name,
        "normalize.version_tuple": normalize.version_tuple,
        "httpx.service_key": httpx.service_key,
        "httpx.product_key": httpx.product_key,
        "domainlist.accepts": domainlist.accepts,
        "identity.literal": literal,
    }


def _assert_rejected(rule, reject: dict) -> None:
    """反例必须**真的被拒** —— 抛 `NormalizeError` 或返回假值都算。"""
    try:
        outcome = rule(*reject["args"])
    except (normalize.NormalizeError, TypeError):
        return
    assert not outcome, f"反例没有被拒：{reject}，得到 {outcome!r}"


# ---------------------------------------------------------------------------
# definition_hash 强制填写（同期必交项 B）
# ---------------------------------------------------------------------------


def test_register_parser_requires_definition_hash(db):
    """反例红→绿：不传 `definition_hash` → 抛错，而且说清缺了会答不出什么。"""
    with pytest.raises(store.InvariantError) as exc:
        store.register_parser(db, "some@1")
    assert "definition_hash" in str(exc.value)
    assert "实现有没有变过" in str(exc.value)
    assert db.execute("SELECT COUNT(*) FROM parser_profiles").fetchone()[0] == 0


def test_register_parser_rejects_blank_definition_hash(db):
    for blank in ("", "   ", None):
        with pytest.raises(store.InvariantError):
            store.register_parser(db, f"blank-{blank!r}@1", definition_hash=blank)


def test_definition_hash_missing_is_rejected_at_the_database_level_too(db):
    """DB 层兜底：绕过应用层的手写 SQL 同样进不来（迁移 003 的触发器）。"""
    with pytest.raises(sqlite3.IntegrityError) as exc:
        db.execute("INSERT INTO parser_profiles(fingerprint, registered_at)"
                   " VALUES ('raw-sql@1', 'now')")
    assert "definition_hash" in str(exc.value)


def test_register_parser_is_idempotent_and_never_overwrites_a_real_hash(db):
    store.register_parser(db, "p@1", profile_id="p", version="1",
                          definition_hash="hash-a")
    store.register_parser(db, "p@1", definition_hash="hash-a")          # 幂等
    store.register_parser(db, "p@1", definition_hash="hash-b")          # 不覆盖
    assert db.execute("SELECT definition_hash FROM parser_profiles"
                      " WHERE fingerprint='p@1'").fetchone()[0] == "hash-a"


def test_name_only_registration_is_explicit_and_can_be_upgraded(db):
    """名称级登记用**显式常量**标注，之后被真实现 hash 回填。

    这条区分是这笔债的核心：项目 scope 上钉的 fingerprint 只有一个名字，
    硬要 hash 只能自欺；而 `ingest` 手里有解析器类，它的 hash 是真值。
    """
    store.register_parser(db, "nmap@1", definition_hash=store.UNHASHED_PARSER)
    assert db.execute("SELECT definition_hash FROM parser_profiles"
                      " WHERE fingerprint='nmap@1'").fetchone()[0] == \
        store.UNHASHED_PARSER

    store.register_parser(db, "nmap@1", definition_hash="realimpl")
    assert db.execute("SELECT definition_hash FROM parser_profiles"
                      " WHERE fingerprint='nmap@1'").fetchone()[0] == "realimpl"


def test_legacy_null_definition_hash_is_backfilled_on_next_touch(db):
    """老库迁移过来的行 hash 为空 → 下次被触碰时回填，而不是永远答不出来。

    测试里先 **DROP 掉那条 INSERT 触发器**才造得出这样一行 —— 它只可能来自
    迁移之前（迁移 003 之后，NULL 根本写不进来，这正是那条触发器的目的）。
    """
    db.execute("DROP TRIGGER trg_parser_requires_definition_hash")
    db.execute("INSERT INTO parser_profiles(fingerprint, registered_at)"
               " VALUES ('legacy@1', '2026-01-01T00:00:00Z')")
    assert db.execute("SELECT definition_hash FROM parser_profiles"
                      " WHERE fingerprint='legacy@1'").fetchone()[0] is None
    store.register_parser(db, "legacy@1", definition_hash="now-we-know")
    assert db.execute("SELECT definition_hash FROM parser_profiles"
                      " WHERE fingerprint='legacy@1'").fetchone()[0] == "now-we-know"


def test_ingest_freezes_a_real_definition_hash_on_every_job(db, project, tmp_path):
    """每个 job 冻结自己的 `parser_fingerprint`，而指纹背后的 hash 是**真值**。"""
    path = tmp_path / "d.txt"
    path.write_text("app.example.com\n", encoding="utf-8")
    report = ingest.ingest_file(db, options(db, project, tmp_path), path)

    row = db.execute("SELECT definition_hash FROM parser_profiles WHERE fingerprint=?",
                     (report["parser"],)).fetchone()
    assert row is not None
    assert row[0] and row[0] != store.UNHASHED_PARSER
    assert len(row[0]) == 64                       # sha256 hex
    assert report["definition_hash"] == row[0]
    assert "definition_drift" not in report


def test_parser_drift_is_reported_when_the_implementation_changes(db, project,
                                                                  tmp_path):
    """同一 fingerprint 背后的实现变过 → 报告里说得出来（这正是那笔债要买的东西）。"""
    path = tmp_path / "d.txt"
    path.write_text("app.example.com\n", encoding="utf-8")
    first = ingest.ingest_file(db, options(db, project, tmp_path), path)

    db.execute("UPDATE parser_profiles SET definition_hash='something-else'"
               " WHERE fingerprint=?", (first["parser"],))
    clone = tmp_path / "e.txt"
    clone.write_text("beta.example.com\n", encoding="utf-8")
    second = ingest.ingest_file(db, options(db, project, tmp_path), clone)

    assert "definition_drift" in second
    assert "实现变过" in second["definition_drift"]


# ---------------------------------------------------------------------------
# 框架契约与发现机制
# ---------------------------------------------------------------------------


def test_ingest_parse_context_satisfies_the_framework_contract():
    """`ingest.ParseContext` 满足 §5.3 的契约 —— 一份契约、一个实现，不是两份实现。"""
    ctx = ingest.ParseContext(project_id=1, source_name="x")
    assert isinstance(ctx, base.ParseContext)
    assert isinstance(base.OfflineContext(), base.ParseContext)


def test_load_parsers_finds_both_parsers_and_retires_the_bridge():
    parsers, source = ingest.load_parsers()
    assert source == "magpie.core.parsers"
    assert [p.tool for p in parsers] == ["domainlist", "httpx"]


def test_sniffing_separates_the_two_formats_cleanly(db, project, tmp_path):
    """仲裁必须**分得开**：两个解析器对同一份文件得分差距 ≥ 0.15。

    分不开的后果是整批文件被推给人工裁决（§5.4），"先窄后宽"就永远宽不起来。
    """
    parsers, _ = ingest.load_parsers()
    samples = {
        # 干净的清单（黄金夹具刻意留了坏行，不适合验"分得开"——验的是另一件事）
        "domainlist": "".join(f"host{i:02d}.example.com\n" for i in range(20)),
        "httpx": (FIXTURES / "httpx" / "1" / "input.jsonl").read_text(encoding="utf-8")[:2000],
    }
    for name, sample in samples.items():
        ranked = ingest.sniff_rank(sample[:ingest.SAMPLE_BYTES], f"{name}.txt",
                                   parsers)
        top, second = ranked[0], ranked[1]
        assert top["tool"] == name, ranked
        assert top["score"] >= ingest.SNIFF_ADOPT_SCORE, ranked
        assert top["score"] - second["score"] >= ingest.SNIFF_ADOPT_MARGIN, ranked
        assert ingest.arbitrate(ranked)["adopted"]["tool"] == name, ranked


def test_a_dirty_list_falls_to_human_review_instead_of_being_guessed(db, project,
                                                                     tmp_path):
    """反面：一份**一半是垃圾**的清单不该被硬猜成域名清单，而该进人工裁决（§5.4）。

    这是"先窄后宽"的刹车：嗅探分数低、或与第二名拉不开差距时，宁可留一条待办
    也不猜一个解析器 —— 猜错的代价是把垃圾灌进资产镜头，而且没人知道。
    """
    path = tmp_path / "dirty.txt"
    path.write_text("app.example.com\nnot a domain\n- bad\nlocalhost\n"
                    "also bad\napi.example.com\n", encoding="utf-8")
    report = ingest.ingest_file(db, options(db, project, tmp_path), path)

    assert report["state"] == "needs_confirmation"
    assert report["inbox_item_id"], "待人工确认必须真的留下一条 Inbox"
    assert "不采用任何解析器" in report["skip_reason"]
    assert report["counts"]["records"] == 0


def test_parsers_never_list_the_whole_file():
    """流式：`parse()` 拿到的是迭代器，不是列表 —— 它不许 `list(lines)`。

    这里给一个**会抗议的**迭代器：一旦有人把它物化成列表，`__len__` 就会炸。
    """
    class NotAList:
        def __init__(self, items):
            self._items = items
            self.materialized = False

        def __iter__(self):
            return iter(self._items)

        def __len__(self):
            raise AssertionError("parse() 把行迭代器物化成列表了 —— 大文件会吃内存")

    for cls in (domainlist.DomainListParser, httpx.HttpxParser):
        ctx = base.OfflineContext()
        list(cls().parse(NotAList(["app.example.com"]), ctx))


def test_parser_class_attributes_are_complete():
    """§5.3 的四个类属性一个都不能少 —— `ingest` 按鸭子类型认它们。"""
    for cls in (domainlist.DomainListParser, httpx.HttpxParser):
        assert cls.tool and cls.format_version
        assert isinstance(cls.known_fields, frozenset)
        assert 0.0 < cls.max_confidence <= 1.0
        assert cls().fingerprint == f"{cls.tool}@{cls.format_version}"
        score = cls.sniff("", "x")
        assert isinstance(score, float) and 0.0 <= score <= 1.0


def test_parser_modules_stay_out_of_the_write_entry():
    """硬线 2 的**运行时**侧检查（静态那一半在 `check.py lint` 的 PARSE-RO）。

    解析器一旦能 import 写入口，"一切入口走管线"就没了 —— 而那是 R1 的全部价值。
    """
    import ast

    for module in (domainlist, httpx, base):
        source = Path(module.__file__).read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                prefix = "." * node.level + (node.module or "")
                imported.add(prefix)
        forbidden = [m for m in imported
                     if m.split(".")[-1] in {"ops", "store", "sqlite3"}
                     or m == "sqlite3"]
        assert not forbidden, f"{module.__name__} 引了写入口：{forbidden}"
