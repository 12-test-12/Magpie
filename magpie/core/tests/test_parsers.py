"""解析器框架与各来源解析器的验收回归网（开发实施计划 2.4 + 3.1 + 3.2）。

**每一条验收标准对应一个用例**，标准写在哪、用例就在哪：

    | 黄金夹具   | 每个解析器各自逐条比对 expected.json，**逐字段**而非只看条数     |
    | 负向夹具   | 每一条都**不崩**，且产出可读的坏行报告（不是 Python 堆栈）        |
    | 未知字段   | 喂一条含新字段的行 → 报告出现「检测到新字段 x（N 行）」           |
    | 词表       | 未登记 kind 被拒（反例红→绿）；每个登记的 kind 三件套齐全         |
    | 硬线       | 解析器只产出 (kind, key)，绝不产出数据库自增 id                   |
    | definition_hash | 缺失即报错（反例红→绿）                                     |
    | 发现       | `magpie.core.parsers` 一落地，`parsers_bridge` 自动退役           |
    | 去重（3.1）| httpx 与 nmap 对同一 `nginx/1.24.0` **只产出一个 product 实体**    |
    | 重复命中（3.2）| 同一份 nuclei 文件导两遍 `added=0` 且**逐行指纹不变**；undo 之后|
    |            | vuln 实体 / 关系 / severity 全部归零，不留待决 Inbox 项          |

2.4 立的是框架与两个解析器（domainlist / httpx）；3.1 加的是 nmap；3.2 加的是
nuclei。各步的用例集中在文件后半对应的小节里（"与 httpx 撞车" / "CPE 拆解" / "sniff 分离" / 负向输入）。

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
from magpie.core.parsers import base, dirsearch, domainlist, ffuf, gau, httpx, \
    katana, nmap, nuclei

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
          ("httpx", httpx.HttpxParser),
          ("nmap", nmap.NmapParser),
          ("nuclei", nuclei.NucleiParser),
          ("gau", gau.GauParser),
          ("katana", katana.KatanaParser),
          ("dirsearch", dirsearch.DirsearchParser),
          ("ffuf", ffuf.FfufParser)]


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
    """反例红→绿：未登记的 kind **必然被拒**（不是靠自觉）。

    ⚠️ 3.3 把这条反例从 `endpoint` 换成了 `apischema`：`endpoint` 在 3.3 被正式
    登记了，而**一个被转正的反例就不再是反例** —— 它会让这条测试永远绿，同时
    悄悄失去它唯一的作用。所以换一个仍然未登记的 kind 接着守
    （`test_unregistered_kind_gets_a_readable_python_error_too` 用的也是它）。
    `apischema` 要等到 3.4 的接口来源落地才会被登记，那时它同样要再换一次。
    """
    with pytest.raises(sqlite3.IntegrityError) as exc:
        store.new_entity(db, project_id=project, kind="apischema", entity_key="x")
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
        # 3.2 的 vuln 身份规则（登记在 `kind_registry.identity_rule` 里）。
        # `vuln_key` 是**身份规则本身**，`normalize_vuln_target` 是它的第二个
        # 落点 —— 夹具要能分别证明"目标怎么规范化"与"身份怎么拼"。
        "normalize.normalize_vuln_target": normalize.normalize_vuln_target,
        "normalize.vuln_key": normalize.vuln_key,
        # 3.3 的 endpoint 身份规则（登记在 `kind_registry.identity_rule` 里）。
        # 折叠规则本身在 `normalize.SEGMENT_TEMPLATES`，这个函数是它的字符串面。
        "normalize.endpoint_key": normalize.endpoint_key,
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


def test_load_parsers_finds_every_parser_and_retires_the_bridge():
    """发现机制是**按包扫描 + 鸭子类型**：3.1 只新增了一个文件，没有在别处登记。

    这里的期望值是**写死的清单**（不是 `>=`）：多出一个解析器意味着有人新增了
    格式，而那件事必须被看见 —— 顺带发现 `format_version` 撞车、`tool` 重名。
    新增解析器时改这一行是**对的**，它是一次显式的确认。
    """
    parsers, source = ingest.load_parsers()
    assert source == "magpie.core.parsers"
    assert [p.tool for p in parsers] == [
        "dirsearch", "domainlist", "ffuf", "gau", "httpx", "katana", "nmap",
        "nuclei"]


def test_sniffing_separates_the_formats_cleanly(db, project, tmp_path):
    """仲裁必须**分得开**：解析器对同一份文件得分差距 ≥ 0.15。

    分不开的后果是整批文件被推给人工裁决（§5.4），"先窄后宽"就永远宽不起来。
    3.1 加了 nmap 之后这条更值得跑了：XML 与 JSONL 与纯文本三种形状，任何两个
    互相误判都会让**新来源**（而不是老来源）掉进人工裁决 —— 那正是"宽不起来"
    的那个方向。
    """
    parsers, _ = ingest.load_parsers()
    samples = {
        # 干净的清单（黄金夹具刻意留了坏行，不适合验"分得开"——验的是另一件事）
        "domainlist": "".join(f"host{i:02d}.example.com\n" for i in range(20)),
        "httpx": (FIXTURES / "httpx" / "1" / "input.jsonl").read_text(encoding="utf-8")[:2000],
        "nmap": (FIXTURES / "nmap" / "1" / "input.xml").read_text(encoding="utf-8"),
        # nuclei 与 httpx **都是 JSONL**，而 nuclei 的行也带 `host`（3.2 之前
        # httpx 的判据是"有 host 就算我的"）。这一条就是那句话的反例。
        "nuclei": (FIXTURES / "nuclei" / "1" / "input.jsonl").read_text(encoding="utf-8"),
        # 3.3 的四个"路径列表"来源：两种文本（gau / dirsearch）与两种 JSON
        # （katana 的 JSONL / ffuf 的整份 JSON）。**四个两两都要拉得开** ——
        # 它们的判据刻意互不相交，抢错了不会有别的测试发现（这一条就是那个测试）。
        "gau": (FIXTURES / "gau" / "1" / "input.txt").read_text(encoding="utf-8"),
        "katana": (FIXTURES / "katana" / "1" / "input.jsonl").read_text(encoding="utf-8"),
        "dirsearch": (FIXTURES / "dirsearch" / "1" / "input.txt").read_text(encoding="utf-8"),
        "ffuf": (FIXTURES / "ffuf" / "1" / "input.json").read_text(encoding="utf-8"),
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

    for cls in (domainlist.DomainListParser, httpx.HttpxParser, nmap.NmapParser,
                nuclei.NucleiParser, gau.GauParser, katana.KatanaParser,
                dirsearch.DirsearchParser, ffuf.FfufParser):
        ctx = base.OfflineContext()
        list(cls().parse(NotAList(["app.example.com"]), ctx))


def test_parser_class_attributes_are_complete():
    """§5.3 的四个类属性一个都不能少 —— `ingest` 按鸭子类型认它们。"""
    for cls in (domainlist.DomainListParser, httpx.HttpxParser, nmap.NmapParser,
                nuclei.NucleiParser, gau.GauParser, katana.KatanaParser,
                dirsearch.DirsearchParser, ffuf.FfufParser):
        assert cls.tool and cls.format_version
        assert isinstance(cls.known_fields, frozenset)
        assert 0.0 < cls.max_confidence <= 1.0
        assert cls().fingerprint == f"{cls.tool}@{cls.format_version}"
        score = cls.sniff("", "x")
        assert isinstance(score, float) and 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# 3.1 · nmap：与 httpx **撞车**（本步的核心交付标准）
# ---------------------------------------------------------------------------
#
# 这一组是 3.1 最值得看的那几行。"两个解析器各自都产出 product"是**不够**的：
# 计划书 §4.2 要的是「哪些还在用 Nginx 1.18」这样的一个查询，而那个查询只有在
# 两个来源说**同一件事**、落在**同一个实体**上时才成立。所以这里不看"有没有
# 产出"，看的是"产出的是不是同一个"。

#: 对撞夹具：**同一台机器**（203.0.113.10）的两个来源，两边都以 IP 为身份起点。
#:
#:   * `httpx-host.jsonl` —— HTTP 探测，`Nginx:1.24.0` 来自 `tech[]`；
#:   * `nmap-host.xml`   —— 端口扫描（`-sV`），`cpe:/a:igor_sysoev:nginx:1.24.0`。
#:
#: 为什么两边都用 IP 而不是域名：目标清单是 IP 时两个工具的 `service` key 才会
#: 逐字相同（`203.0.113.10:80`）。这也让这条用例**不依赖** httpx 那条已知的
#: kind 缺陷 —— httpx 会把 IP 字面量建成一个 `domain` 实体（`domainlist.py` 里
#: 有一段显式守卫正是为了防它，httpx 没有）。缺陷本身已单独报给发起人，
#: 修它会让既有的 httpx 黄金夹具与老库的实体 kind 变化，所以不在本步范围里。
_COLLISION = FIXTURES / "collision" / "httpx-host.jsonl"
_COLLISION_NMAP = FIXTURES / "collision" / "nmap-host.xml"


def test_httpx_and_nmap_reach_the_same_product_entity(db, project, tmp_path):
    """**对撞用例**：同一台机器、两个来源、一个 `product` 实体。

    为什么它比对两边的记录条数重要：两个解析器可以各自都产出
    `product(nginx@1.24.0)`，然后因为 `service` 的 key 差一个字节（`host:80`
    vs `host:080`、域名 vs IP）而落在**两个** service 上 —— 那时"哪些还在用
    nginx 1.24.0"会答出两个服务，而它们其实是同一个。所以这里断言的是
    **实体数**（product / service 各只有一个），不是记录数。
    """
    opts = options(db, project, tmp_path)
    httpx_report = ingest.ingest_file(db, opts, _COLLISION)
    nmap_report = ingest.ingest_file(db, opts, _COLLISION_NMAP)
    assert httpx_report["state"] == "done", httpx_report
    assert nmap_report["state"] == "done", nmap_report
    assert httpx_report["parser"] == "httpx@1"
    assert nmap_report["parser"] == "nmap@1"

    def keys(kind: str) -> set[str]:
        return {str(row[0]) for row in db.execute(
            "SELECT ek.entity_key FROM entity_keys ek"
            "  JOIN entities e ON e.id = ek.entity_id"
            " WHERE e.project_id=? AND e.kind=? AND e.is_current=1", (project, kind))}

    products = keys("product")
    assert "nginx@1.24.0" in products, products
    assert len([p for p in products if p.startswith("nginx")]) == 1, (
        f"同一个 nginx 1.24.0 产出了多个 product 实体：{sorted(products)}")

    # 服务也只有一个：`host:port` 的身份规则是共享的，两边的 host 同一个写法。
    services = keys("service")
    assert "203.0.113.10:80" in services, services

    # 两个来源各留一条 `runs` **贡献**（那是"为什么信它"的证据链），而缓存行
    # 只有**一条**关系 —— 事实层与投影层的区别就在这里，也正是"不产生重复
    # product 实体"这句话在数据层的落点。
    def scalar(sql: str) -> int:
        return int(db.execute(sql, (project,)).fetchone()[0])

    # `entity_keys` 是 (project, key) 唯一的**身份标签**，`entities.is_current`
    # 才是"这条缓存行当前成立"。两个概念分开读，计数才不会被历史行放大。
    cache_rows = scalar(
        "SELECT COUNT(*) FROM relations r"
        " JOIN entity_keys se ON se.entity_id=r.src_id"
        " JOIN entity_keys de ON de.entity_id=r.dst_id"
        " JOIN entities s ON s.id=r.src_id AND s.is_current=1"
        " JOIN entities d ON d.id=r.dst_id AND d.is_current=1"
        " WHERE r.project_id=? AND se.entity_key='203.0.113.10:80'"
        "   AND de.entity_key='nginx@1.24.0' AND r.rel='runs'")
    assert cache_rows == 1, "同一对 (service, product) 在缓存层只能有一条 runs"
    # `contributions.subject_id` 是**多态引用**（entity / observation / relation
    # 各有自己的自增空间），所以 join 必须带上 `subject_kind` —— 只按 id 连的话
    # 一条 id 撞上某条观测的贡献，计数会莫名其妙地大一个数量级。
    contributions = scalar(
        "SELECT COUNT(*) FROM contributions c JOIN relations r ON r.id=c.subject_id"
        " JOIN entity_keys se ON se.entity_id=r.src_id"
        " WHERE c.project_id=? AND c.subject_kind='relation' AND c.contribution_kind='relation'"
        "   AND se.entity_key='203.0.113.10:80' AND r.rel='runs'")
    assert contributions == 2, "两个来源各贡献了一次 runs"


def test_nmap_does_not_fold_really_different_versions(db, project, tmp_path):
    """反向：**真的不同的版本不许被折叠**（`1.24.0` 与 `1.24.1` 是两个实体）。

    没有这一条，"去重"可以用"把版本丢掉"来作弊 —— 那会得到一个更小、
    更整齐、也更没用的图。所以对撞用例必须成对：正向收敛、反向不收敛。
    """
    report = ingest.ingest_file(db, options(db, project, tmp_path), _COLLISION)
    assert report["state"] == "done", report

    xml = (FIXTURES / "nmap" / "1" / "input.xml").read_text(encoding="utf-8")
    bumped = xml.replace("nginx:1.24.0", "nginx:1.24.1")
    other = tmp_path / "bumped.xml"
    other.write_text(bumped, encoding="utf-8")
    assert ingest.ingest_file(db, options(db, project, tmp_path), other)["state"] == "done"

    products = {str(row[0]) for row in db.execute(
        "SELECT ek.entity_key FROM entity_keys ek JOIN entities e ON e.id=ek.entity_id"
        " WHERE e.project_id=? AND e.kind='product'", (project,))}
    assert {"nginx@1.24.0", "nginx@1.24.1"} <= products, products


def test_nmap_product_key_is_the_httpx_one_not_a_second_copy():
    """身份规则**只有一份实现**：nmap 直接用 `httpx.product_key` / `service_key`。

    这条断言看着像废话，但它挡着的是一种很自然的写法 —— "在 nmap.py 里也写一个
    `product_key`，反正逻辑一样"。两份实现迟早在别名表或大小写上分叉，而分叉
    之后没有答案的是"哪个才对"。所以这里比对的是**函数对象本身**。
    """
    assert nmap.product_key is httpx.product_key
    assert nmap.service_key is httpx.service_key


# ---------------------------------------------------------------------------
# 3.1 · CPE 拆解（本步与 httpx 拉开差距的地方）
# ---------------------------------------------------------------------------


CPE_CASES = [
    ("cpe:/a:igor_sysoev:nginx:1.24.0", "nginx@1.24.0", "a", "igor_sysoev", ""),
    # 2.2 的双斜杠写法：同样的东西，另一种正文。
    ("cpe://a:igor_sysoev:nginx:1.24.0", "nginx@1.24.0", "a", "igor_sysoev", ""),
    # 厂商前缀折叠走的是与 httpx 同一张别名表（Apache httpd → httpd）。
    ("cpe:/a:apache:httpd:2.4.52", "httpd@2.4.52", "a", "apache", ""),
    # 没有版本 = **另一个实体**（不是"版本未知的 nginx"）。
    ("cpe:/a:nginx:nginx", "nginx", "a", "nginx", ""),
    # 版本区间：身份取下界，区间单独回传 —— 折成 2.4.49 是把"nmap 只知道一个
    # 范围"谎报成"就是 2.4.49"。
    ("cpe:/a:apache:httpd:2.4.49-2.4.51", "httpd@2.4.49", "a", "apache",
     "2.4.49-2.4.51"),
    # 预发布**不是**区间（`rc1` 过不了 version_tuple，于是走预发布那条路）。
    ("cpe:/a:nginx:nginx:1.24.0-rc1", "nginx@1.24.0-rc1", "a", "nginx", ""),
]


@pytest.mark.parametrize("raw,key,part,vendor,version_range", CPE_CASES)
def test_cpe_is_broken_down_into_vendor_product_version(raw, key, part, vendor,
                                                       version_range):
    parts = nmap.cpe_breakdown(raw)
    assert parts is not None, raw
    assert parts["key"] == key
    assert parts["part"] == part
    assert parts["vendor"] == vendor
    assert parts["version_range"] == version_range


def test_cpe_part_is_kept_but_never_enters_the_identity():
    """`cpe:/o:` 与 `cpe:/a:` 不得混为一谈 —— 但区分**不在 key 里**。

    两件事同时成立才是对的：part 必须被保留（操作系统与硬件不该与中间件挤在
    一起），而 key 必须仍然只是"产品名[@版本]" —— 把 part 拉进身份会让同一个
    nginx 因为写法差异裂成两个实体，而「哪些还在用 Nginx 1.18」正是靠那个身份
    才问得出来。所以这里断言"同一个产品名，两份 CPE，两个不同的 part"。
    """
    app = nmap.cpe_breakdown("cpe:/a:linux:linux_kernel:5.15")
    os_cpe = nmap.cpe_breakdown("cpe:/o:linux:linux_kernel:5.15")
    assert app is not None and os_cpe is not None
    assert app["part"] == "a" and os_cpe["part"] == "o"
    assert app["key"] == os_cpe["key"] == "linux_kernel@5.15"

    hardware = nmap.cpe_breakdown("cpe:/h:hp:proliant_dl380")
    assert hardware is not None and hardware["part"] == "h"
    assert hardware["key"] == "proliant_dl380"


def test_cpe_rejects_what_is_not_a_cpe():
    """不是 CPE 就返回 None（不猜、不抛）—— 解析器据此报一条可读坏行。

    `cpe:2.3:` 的**格式化**写法不在本步范围内（nmap 的 `-oX` 输出不用它）。
    这里显式把它钉成 None：它比"忘了处理"更接近"决定不支持"，而决定必须
    被写在断言里，否则下一个人会以为那是个 bug 顺手补上。
    """
    for raw in ("cpe:2.3:a:nginx:nginx:1.24.0:*:*:*:*:*:*:*", "garbage", "",
                None, "cpe:/", "cpe:/a:vendor:"):
        assert nmap.cpe_breakdown(raw) is None, raw


def test_cpe_in_a_service_becomes_a_product_entity(db, project, tmp_path):
    """CPE 落地：`cpe:/o:` 那一份同样提升成 product，且 part 留在属性里。"""
    report = ingest.ingest_file(db, options(db, project, tmp_path),
                                FIXTURES / "nmap" / "1" / "input.xml")
    assert report["state"] == "done", report
    rows = {str(r[0]): str(r[1]) for r in db.execute(
        "SELECT semantic_field, value FROM observations WHERE project_id=?"
        " AND entity_id IN (SELECT id FROM entity_keys ek JOIN entities e"
        "   ON e.id=ek.entity_id WHERE ek.entity_key='203.0.113.11:22'"
        "   AND e.kind='service')", (project,))}
    assert rows.get("cpe") in ("cpe:/o:linux:linux_kernel:5.15",
                               "cpe:/h:hp:proliant_dl380")
    assert rows.get("cpe_vendor") in ("linux", "hp")


# ---------------------------------------------------------------------------
# 3.1 · sniff：nmap XML 与另外两种形状互不误判
# ---------------------------------------------------------------------------


def test_nmap_sniff_only_claims_nmap_xml():
    """判据是"nmap 自己的签名"，不是"它是不是 XML"。

    按"是 XML"给分的下场：一份 Burp / Nessus 的 XML 会被认领，然后在解析时
    逐行报错 —— 那比"不采用、交人工裁决"糟得多（§5.4）。
    """
    sample = (FIXTURES / "nmap" / "1" / "input.xml").read_text(encoding="utf-8")
    assert nmap.NmapParser.sniff(sample, "scan.xml") >= 0.9
    # 别的 XML 一律不认领（哪怕它带着 <host> 这个词）。
    assert nmap.NmapParser.sniff(
        '<?xml version="1.0"?><nessus><ReportHost name="x"/></nessus>', "x.xml") == 0.0
    assert nmap.NmapParser.sniff('<?xml version="1.0"?><report/>', "x.xml") == 0.0
    # JSONL / 纯文本同样是 0 —— 它们各自有解析器认领。
    assert nmap.NmapParser.sniff('{"host":"a.example.com"}', "x.jsonl") == 0.0
    assert nmap.NmapParser.sniff("a.example.com\nb.example.com\n", "x.txt") == 0.0


def test_sniff_is_cheap_for_the_files_it_must_not_claim():
    """`sniff` 在每个文件上都会被调用一次（`sniff_rank`），所以它必须便宜。

    这条守的是 bench 里那条 10 万观测导入契约（§9.2 ⑤，余量约 15%）：
    一个在 64 KiB 样本上做 O(文本长度) 解析的嗅探器，会在**每一份** httpx
    文件上白跑一遍 XML 解析。实测口径：10 万行 JSONL 样本的判定 < 50ms。
    """
    import time

    sample = (FIXTURES / "httpx" / "1" / "input.jsonl").read_text(encoding="utf-8")
    blob = (sample * (ingest.SAMPLE_BYTES // len(sample) + 1))[:ingest.SAMPLE_BYTES]
    t0 = time.perf_counter()
    for _ in range(20):
        nmap.NmapParser.sniff(blob, "big.jsonl")
    elapsed = (time.perf_counter() - t0) / 20
    assert elapsed < 0.05, f"nmap.sniff 在 JSONL 样本上耗时 {elapsed * 1000:.1f}ms"


# ---------------------------------------------------------------------------
# 3.1 · nmap 的负向输入（每条都不崩、都有可读提示）
# ---------------------------------------------------------------------------

_HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE nmaprun>\n'
         '<nmaprun scanner="nmap" args="nmap -sV -oX x.xml 10.0.0.0/24"'
         ' start="0" version="7.94" xmloutputversion="1.05">\n')

NMAP_NEGATIVE = [
    ("nmap-empty", "", "空 XML 文件"),
    ("nmap-nohost", _HEAD + "<runstats><finished time=\"0\"/></runstats>\n</nmaprun>\n",
     "XML 合法但没有 <host>"),
    ("nmap-damaged", _HEAD + "<host><status state=\"up\"\n", "截断的 XML"),
    ("nmap-notnmap", '<?xml version="1.0"?>\n<other><host/></other>\n',
     "合法 XML 但不是 nmap"),
    ("nmap-gbk", _HEAD + '<host><status state="up"/><address addr="10.0.0.7"'
     ' addrtype="ipv4"/><hostnames><hostname name="数据库.example.com"'
     ' type="PTR"/></hostnames><ports/></host>\n</nmaprun>\n', "非 UTF-8（GBK）"),
]


@pytest.mark.parametrize("name,payload,label", NMAP_NEGATIVE,
                         ids=[c[0] for c in NMAP_NEGATIVE])
def test_nmap_negative_inputs_never_crash(db, project, tmp_path, name, payload, label):
    """走**完整的 ingest 管线**（不是只调解析器）：可读报告，不是 Python 堆栈。"""
    path = tmp_path / f"{name}.xml"
    path.write_text(payload, encoding="utf-8" if name != "nmap-gbk" else "gbk")
    report = ingest.ingest_file(db, options(db, project, tmp_path), path)

    assert isinstance(report, dict), label
    assert report["state"] in {"done", "partial", "skipped", "failed",
                              "needs_confirmation", "cancelled"}, label
    assert "Traceback" not in json.dumps(report, ensure_ascii=False), label


def test_nmap_empty_xml_is_not_reported_as_a_crash(tmp_path):
    """空文件不该被报成"XML 损坏"—— 它连一个元素都没有，那是误报。

    判据是"坏行为零**且**说明是人话"：一个 0 字节的文件进仲裁（没有解析器会
    认领它），而不是得到一个 `no element found` 的解析错。
    """
    ctx = base.OfflineContext()
    assert list(nmap.NmapParser().parse([], ctx)) == []
    assert ctx.bad_lines == []
    assert ctx.notes == []


def test_nmap_truncated_xml_is_reported_readably(tmp_path):
    """截断的 XML：**必须报错**，而且行号与原因都是人话。

    这一条挡的是一个很安静的失败：`XMLPullParser` 对未闭合的元素在 `feed()`
    时是"暂未结束"而不是错误 —— 漏掉 `close()` 的话，一份损坏的文件会被报成
    "0 主机 0 坏行"的空扫描，而报告说 `done`。
    """
    ctx = base.OfflineContext()
    text = _HEAD + "<host><status state=\"up\"\n<ports>\n"
    records = list(nmap.NmapParser().parse(text.splitlines(), ctx))
    assert records == []
    assert ctx.bad_lines, "截断的 XML 必须留下坏行，不能静默变成空扫描"
    assert "XML 解析失败" in ctx.bad_lines[0]["reason"]
    assert "Traceback" not in ctx.bad_lines[0]["reason"]


def test_nmap_missing_host_element_is_explained_not_silent(tmp_path):
    """没有 `<host>` 不是格式漂移，也不是坏行 —— 它是一条**说明**。

    主机全部宕机、`--host-timeout` 跳过、或只是 `-sn` 的空结果，都会产出这样
    一份 XML。把它算成 bad_lines 会让 `counts.bad_lines` 这个质量信号在**一份
    完全正常的文件**上报警。
    """
    ctx = base.OfflineContext()
    text = _HEAD + "<runstats/>\n</nmaprun>\n"
    assert list(nmap.NmapParser().parse(text.splitlines(), ctx)) == []
    assert ctx.bad_lines == []
    assert any("没有 <host>" in note for note in ctx.notes), ctx.notes


def test_nmap_sniff_survives_a_truncated_sample():
    """嗅探样本止于 64 KiB —— 大文件在样本里只剩前几行，那几行必须是够用的。

    `probe` 只读文件头，所以一份 10 MB 的 `-oX` 在 `sniff` 眼里就是它的开头。
    `<!DOCTYPE nmaprun` 正好在那一段里（nmap 的输出顺序固定），但这条断言把它
    钉住了：哪天 nmap 改了输出顺序，这里会红，而不是"大文件莫名进人工裁决"。
    """
    head = '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE nmaprun>\n'
    assert nmap.NmapParser.sniff(head, "big.xml") >= 0.9
    assert nmap.NmapParser.sniff(head + "<nmaprun scanner=\"nmap\">\n", "big.xml") >= 0.9


def test_nmap_script_output_is_bounded(db, project, tmp_path):
    """脚本正文**截断入库**：`-sV` 的一条输出动辄几 KB，全存会吃掉性能契约。

    截断而不是丢弃：截断后它仍然是"这个脚本跑过、说了什么"的证据。这里用一条
    真正的超大输出（`ssl-cert` 那种，200 KB）来验，而不是看代码里有没有
    `[:400]` —— 顺便确认解析**不因为这条大正文而崩**。

    正文的位置是 `<script>` 的**文本**而不是属性（nmap 在 `-oX` 里先写
    `<script id="…" output="首行">`，随后把完整输出作为元素文本）。
    """
    big = "A" * 200_000
    text = (_HEAD + '<host><status state="up"/><address addr="10.0.0.7"'
            ' addrtype="ipv4"/><ports><port protocol="tcp" portid="443">'
            '<state state="open"/><service name="ssl" method="probed" conf="10"/>'
            f'<script id="ssl-cert" output="Subject: CN=x">{big}</script>'
            "</port></ports></host>\n</nmaprun>\n")
    path = tmp_path / "big-script.xml"
    path.write_text(text, encoding="utf-8")
    report = ingest.ingest_file(db, options(db, project, tmp_path), path)

    assert report["state"] == "done", report
    rows = [str(r[0]) for r in db.execute(
        "SELECT value FROM observations WHERE project_id=? AND semantic_field='script'",
        (project,))]
    assert rows, "脚本观测丢了"
    for value in rows:
        # 与 `ParseContext.on_bad_line` 的 `raw[:400]` 是同一口径。
        assert len(value) <= nmap.MAX_VALUE_CHARS, (
            f"脚本正文没有截断：{len(value)} 字符")
        assert value.startswith("ssl-cert: "), value[:32]


def test_parser_modules_stay_out_of_the_write_entry():
    """硬线 2 的**运行时**侧检查（静态那一半在 `check.py lint` 的 PARSE-RO）。

    解析器一旦能 import 写入口，"一切入口走管线"就没了 —— 而那是 R1 的全部价值。
    """
    import ast

    for module in (domainlist, httpx, nmap, nuclei, base):
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

# ---------------------------------------------------------------------------
# 3.2 · nuclei：负向输入（每条都不崩、有可读提示、**一条事实都不落**）
# ---------------------------------------------------------------------------
#
# 这一组分两层，因为"一条事实都不落"只在一层上是**真的**：
#
#   * **解析层** —— 坏行一律产出零记录。这是本步负向夹具的主判据；
#   * **管线层** —— 走完整的 `ingest_file`：不崩、报告里没有 Traceback、
#     状态落在已知的那几个里。其中**有两条是"能救回来"的输入**（GBK 可解码、
#     缺 template-id 的行恰好长成 httpx 的形状），它们落事实是**对的**行为，
#     所以那两条不断言"零事实" —— 把对的判成错的，比漏测更糟。

_NUCLEI_GOLDEN = FIXTURES / "nuclei" / "1" / "input.jsonl"

#: 单行上限（与 `nuclei.MAX_LINE_CHARS` 同一口径）。测试读实现里那个常量，
#: 而不是再抄一个数 —— 抄出来的数会在实现调整的那天悄悄变成一句废话。
_HUGE_LINE = "{" + "x" * (nuclei.MAX_LINE_CHARS + 1)


def _finding(template_id="tech-detect", matched_at="https://a.example.com/",
             info=None) -> str:
    """一条**最小可用**的 nuclei 命中；参数用来把它改成各种坏形状。

    刻意用命名参数而不是 `**extra` 合并：`matched_at="nas"` 传进 `**extra`
    会写成一个叫 `matched_at`（下划线）的**新字段**，于是真正的 `matched-at`
    还是好的 —— 那条"反例"其实什么都没反，而它会绿。
    """
    return json.dumps({
        "template-id": template_id,
        "host": "https://a.example.com",
        "matched-at": matched_at,
        "info": info or {"name": "Technology Detection", "severity": "info"},
    }, ensure_ascii=False)


NUCLEI_BAD_LINES = [
    ("nuclei-not-json", ["这不是 nuclei 的输出"], "JSON 解析失败"),
    ("nuclei-not-object", ['["a", "b"]'], "不是 JSON 对象"),
    ("nuclei-no-template-id", [_finding(template_id=None)], "缺 template-id"),
    ("nuclei-no-matched-at", [_finding(matched_at=None)], "缺 matched-at"),
    ("nuclei-bad-target", [_finding(matched_at="nas")], "不是可识别的目标"),
    ("nuclei-huge-line", [_HUGE_LINE], "单行超过"),
]


@pytest.mark.parametrize("name,lines,reason", NUCLEI_BAD_LINES,
                         ids=[c[0] for c in NUCLEI_BAD_LINES])
def test_nuclei_bad_lines_land_nothing_and_say_why(name, lines, reason):
    """**解析层**的"一条事实都不落"：坏行产出零记录 + 一句人话。

    判据三件一起看才是完整的：零记录（没落事实）、恰好一条坏行（报出来了）、
    原文被截断（2 MB 的坏行不该把报告也撑成 2 MB）。
    """
    records, ctx = parse_with(nuclei.NucleiParser, lines)
    assert records == [], f"{name}: 坏行产出了记录"
    assert len(ctx.bad_lines) == 1, ctx.bad_lines
    assert reason in ctx.bad_lines[0]["reason"], ctx.bad_lines[0]["reason"]
    assert "Traceback" not in ctx.bad_lines[0]["reason"]
    assert len(ctx.bad_lines[0]["raw"]) <= nuclei.MAX_VALUE_CHARS


def test_nuclei_blank_lines_are_skipped_not_reported():
    """空行不是坏行 —— 它是 JSONL 里合法的间隔，报成坏行会让质量信号失真。"""
    records, ctx = parse_with(nuclei.NucleiParser, ["", "   ", "\t"])
    assert records == [] and ctx.bad_lines == []


NUCLEI_NEGATIVE_FILES = [
    ("nuclei-empty", b"", "空文件"),
    ("nuclei-not-json", "这不是 nuclei 的输出\n也不是\n".encode("utf-8"), "非 JSON 行"),
    ("nuclei-gbk", _finding(info={"name": "远程代码执行", "severity": "high"})
     .encode("gbk"), "非 UTF-8（GBK）"),
    ("nuclei-huge-line", (_HUGE_LINE + "\n").encode("utf-8"), "超大单行"),
]


@pytest.mark.parametrize("name,payload,label", NUCLEI_NEGATIVE_FILES,
                         ids=[c[0] for c in NUCLEI_NEGATIVE_FILES])
def test_nuclei_negative_inputs_never_crash(db, project, tmp_path, name, payload,
                                            label):
    """走**完整的 ingest 管线**（不是只调解析器）：可读报告，不是 Python 堆栈。"""
    path = tmp_path / f"{name}.jsonl"
    path.write_bytes(payload)
    report = ingest.ingest_file(db, options(db, project, tmp_path), path)

    assert isinstance(report, dict), label
    assert report["state"] in {"done", "partial", "skipped", "failed",
                               "needs_confirmation", "cancelled"}, label
    assert "Traceback" not in json.dumps(report, ensure_ascii=False), label


def test_nuclei_unparsable_input_lands_zero_facts(db, project, tmp_path):
    """**管线层**的"一条事实都不落"：仲裁不出解析器的那两份输入，库里必须干净。

    选的是空文件与非 JSON —— 没有任何解析器会认领它们（nuclei 的 sniff 要求
    `template-id`），所以"进人工裁决、零事实"是这两条唯一的正确结局。

    另外两份**故意不在这一组**：GBK 是能解码的、缺 `template-id` 的行恰好长成
    httpx 的形状 —— 它们落事实是对的（见本节的说明）。
    """
    for name, payload in (("empty", b""),
                          ("not-json", "这不是 nuclei 的输出\n".encode("utf-8"))):
        path = tmp_path / f"neg-{name}.jsonl"
        path.write_bytes(payload)
        report = ingest.ingest_file(db, options(db, project, tmp_path), path)
        assert report["counts"]["records"] == 0, (name, report["counts"])
        assert report["counts"]["added"] == 0, (name, report["counts"])

    for kind in ("vuln", "domain", "ip", "service"):
        assert not _keys(db, project, kind), f"负向输入落了 {kind} 实体"


# ---------------------------------------------------------------------------
# 3.2 · **同模板重复命中不重复计数**（本步的核心交付标准）
# ---------------------------------------------------------------------------
#
# 这一节是 3.2 最值得看的那几行。判据刻意**不是**"有没有产出" —— 一个给每条
# 命中都造一个新实体的实现同样"有产出"。判据是：
#
#   * 同一份文件里同一条命中出现两次 → 库里只有一个实体、一条 severity、一条
#     `found_on`（管线把第二遍记成 `skipped`，一条贡献都不写）；
#   * 同一份文件导两遍 → 第二遍 `added=0`，且**逐行指纹**与第一遍之后完全相同；
#   * undo 之后 → vuln 实体 / 关系 / severity 全部归零，且**不留待决 Inbox 项**。
#
# 没有这三条，"同模板重复命中不重复计数"就只是一句可以被 `hit_count` 冒充的话
# —— 而计数器绕开 contributions 模型，undo 之后会留下幻影行。

#: 黄金夹具里那条**重复命中**（同 template-id、同 matched-at，只有 timestamp
#: 不同）。它在库里不该留下任何第二份东西。
_REPEATED_VULN = "CVE-2021-44228@https://app.example.com:443/"


def _keys(db, project_id: int, kind: str) -> set:
    return {str(r[0]) for r in db.execute(
        "SELECT ek.entity_key FROM entity_keys ek"
        "  JOIN entities e ON e.id = ek.entity_id"
        " WHERE e.project_id=? AND e.kind=? AND e.is_current=1", (project_id, kind))}


def _vuln_fields(db, project_id: int, key: str) -> dict:
    return {str(r[0]): str(r[1]) for r in db.execute(
        "SELECT o.semantic_field, o.value FROM observations o"
        "  JOIN entities e ON e.id = o.entity_id"
        "  JOIN entity_keys ek ON ek.entity_id = o.entity_id"
        " WHERE o.project_id=? AND o.is_current=1 AND e.kind='vuln'"
        "   AND ek.entity_key=?", (project_id, key))}


def _findings_in_fixture() -> int:
    """黄金夹具里**解析器产出**了几条 `vuln` 实体记录（不是库里剩了几条）。"""
    _, expected, _ = load_case("nuclei", "1")
    return sum(1 for r in expected["records"]
               if r["type"] == "entity" and r["kind"] == "vuln")


def _row_counts(db, project_id: int) -> dict:
    """本项目的 实体 / 观测 / 关系 三张缓存表的**当前**行数。"""
    out = {}
    for table in ("entities", "observations", "relations"):
        out[table] = int(db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE project_id=? AND is_current=1",
            (project_id,)).fetchone()[0])
    return out


def _fingerprint(db) -> dict:
    """有效状态的**逐行**指纹：每张表一份 sha256 + 行数（与 `verify_v32r5.py` 同法）。

    比"条数"强的地方在于它逐行比：一个把两行换成另外两行的实现，条数一样、
    指纹不同。这正是"第二次导不留痕"唯一能被机器检验的形式。
    """
    import hashlib

    out = {}
    for table, rows in store.snapshot_effective(db).items():
        blob = json.dumps(rows, sort_keys=True, default=str)
        out[table] = {"rows": len(rows),
                      "sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest()}
    return out


def test_nuclei_repeated_hit_leaves_exactly_one_vuln(db, project, tmp_path):
    """同一份文件里同一条命中出现两次 → 库里只剩**一份**。

    夹具里那条重复命中是刻意的：`hit_count` 那类实现会让它变成 2。这里对着
    库里数：实体 / severity / state / `found_on` 各只有一条。
    """
    report = ingest.ingest_file(db, options(db, project, tmp_path), _NUCLEI_GOLDEN)

    # 夹具里有一条缺 matched-at 的坏行 → `partial`（不是 `done`）。这条状态本身
    # 就是"坏行没有把整份文件废掉"的证据。
    assert report["state"] == "partial", report
    assert report["parser"] == "nuclei@1"
    assert report["counts"]["bad_lines"] == 1, report["counts"]
    # 管线把重复的那些 facts 记成 skipped（records == added + updated + skipped）。
    assert report["counts"]["skipped"] >= 15, report["counts"]
    assert report["reconcile"]["ok"], report["reconcile"]

    assert _findings_in_fixture() == 7, "夹具本身变了：重复命中的那两条还在吗？"
    vulns = _keys(db, project, "vuln")
    assert len(vulns) == 6, f"同模板重复命中留下了第二个实体：{sorted(vulns)}"

    fields = _vuln_fields(db, project, _REPEATED_VULN)
    assert fields["severity"] == "critical"
    assert fields["state"] == "open"
    assert fields["matched_at"] == "https://app.example.com:443/"
    rows = db.execute(
        "SELECT COUNT(*) FROM relations r"
        "  JOIN entity_keys s ON s.entity_id = r.src_id"
        "  JOIN entity_keys d ON d.entity_id = r.dst_id"
        " WHERE r.project_id=? AND r.is_current=1 AND r.rel='found_on'"
        "   AND s.entity_key=? AND d.entity_key='app.example.com:443'",
        (project, _REPEATED_VULN)).fetchone()[0]
    assert rows == 1, "重复命中留下了一条多余的 found_on 关系"

    # 两条不同的漏洞落在**同一个** service 上（第 1、2 条命中都是 app.example.com:443）
    # —— 这是 `service` 身份规则只在 httpx 里实现一次的意义。
    assert "app.example.com:443" in _keys(db, project, "service")
    # network 模板给的是 `host:port`：不替它补 scheme，资产就是那个服务。
    assert "203.0.113.20:3389" in _keys(db, project, "service")
    assert "203.0.113.20" in _keys(db, project, "ip")


def test_nuclei_same_file_twice_adds_nothing_and_leaves_no_trace(db, project,
                                                                 tmp_path):
    """**核心交付标准**：同一份 nuclei 文件导两遍 → 第二遍 `added=0`、指纹不变。

    ⚠️ 第二遍走的是 sha256 幂等短路（同文件同指纹 → 一行未写）。这**不足以**
    证明事实层的去重，所以下一条用例换一份不同字节、同一批事实的副本再验一次
    —— 两条合起来才是完整的：一条证"重导不留痕"，一条证"新事实才会加计数"。
    """
    ingest_opts = options(db, project, tmp_path)
    first = ingest.ingest_file(db, ingest_opts, _NUCLEI_GOLDEN)
    assert first["counts"]["added"] > 0, first["counts"]
    after_first = _fingerprint(db)

    again = ingest.ingest_file(db, ingest_opts, _NUCLEI_GOLDEN)
    assert again["state"] == "skipped"
    assert again["job_id"] == first["job_id"]
    assert again["counts"]["added"] == 0, again["counts"]
    assert again["counts"]["records"] == 0, again["counts"]
    assert _fingerprint(db) == after_first, "第二次导入在库里留了痕"

    # 换个文件名、字节相同 → 仍然按 sha256 短路（身份是内容，不是文件名）。
    cloned = tmp_path / "renamed-scan.jsonl"
    cloned.write_bytes(_NUCLEI_GOLDEN.read_bytes())
    third = ingest.ingest_file(db, ingest_opts, cloned)
    assert third["state"] == "skipped" and third["job_id"] == first["job_id"]
    assert _fingerprint(db) == after_first


def test_nuclei_reimport_of_a_changed_file_creates_no_new_rows(db, project,
                                                               tmp_path):
    """不同字节、**同一批事实** → 一行新数据都不该出现。

    这一条补上了上一条的缺口：sha256 短路没被触发（字节真的变了），去重只能靠
    **事实键**。判据是逐表行数 —— 不是逐行指纹，因为重新导入会把 `observed_at`
    的区间上界往后推（那是一个真实发生过的观察时刻，本来就该动）。

    `added` 这里刻意**不**断言：`ingest` 的 added / updated 判定有一个已实测的
    缺陷（`_fact_key` 返回 `(类别, 键)`，而 `_existing_facts` 给的是不带类别的
    键，两边永远对不上 → `updated` 是一条走不到的分支、`added` 把重复行也算成
    新增）。它属于 2.5 的账、已单独报给发起人；**行数**这一层不受它影响，
    而"有没有留下幻影行"正是本步要守的那件事。
    """
    ingest_opts = options(db, project, tmp_path)
    first = ingest.ingest_file(db, ingest_opts, _NUCLEI_GOLDEN)
    assert first["counts"]["added"] > 0
    rows_before = _row_counts(db, project)

    lines = _NUCLEI_GOLDEN.read_text(encoding="utf-8").splitlines()
    mutated = []
    for line in lines:
        obj = json.loads(line)
        if obj.get("timestamp"):
            obj["timestamp"] = "2026-09-20T00:00:00.000000Z"   # 改一个不入库的字段
        mutated.append(json.dumps(obj, ensure_ascii=False))
    mutated.append(mutated[0])                     # 再多一次同模板重复命中
    other = tmp_path / "scan-again.jsonl"
    other.write_text("\n".join(mutated) + "\n", encoding="utf-8")

    again = ingest.ingest_file(db, ingest_opts, other)
    assert again["state"] == "partial", again
    assert again["counts"]["records"] > first["counts"]["records"]

    rows_after = _row_counts(db, project)
    drifted = {k: (rows_before[k], rows_after[k]) for k in rows_before
               if rows_before[k] != rows_after[k]}
    assert not drifted, f"重导（不同字节、同一批事实）造出了新行：{drifted}"


def test_nuclei_undo_removes_every_vuln_fact_and_leaves_no_inbox(db, project,
                                                                 tmp_path):
    """`undo` 该 job → vuln 实体 / 关系 / severity 全部归零，**不留待决 Inbox 项**。

    "归零"用的是逐行指纹而不是"某张表没有行"：撤销之后 `entities` 里的 vuln
    行还在（它是历史事实），但 `is_current=0` —— 指纹比的是**有效状态**，
    所以它答的正是"现在库里还剩什么"。

    ⚠️ 这一条是 `hit_count` 那类实现唯一会露馅的地方：计数器不属于任何 job 的
    贡献，undo 时没人会把它减回去，而**现有测试只断言它读得对**。所以这里同时
    数一次 inbox —— 一个残留的"待人工确认"同样算没撤干净。
    """
    baseline = _fingerprint(db)
    report = ingest.ingest_file(db, options(db, project, tmp_path), _NUCLEI_GOLDEN)
    assert _keys(db, project, "vuln"), "先得有东西可撤"

    ops.undo(db, job_id=report["job_id"], actor="test")

    assert _keys(db, project, "vuln") == set()
    assert _keys(db, project, "service") == set()
    assert _keys(db, project, "product") == set()
    assert db.execute(
        "SELECT COUNT(*) FROM observations WHERE project_id=? AND is_current=1"
        " AND semantic_field IN ('severity','state')", (project,)).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM relations WHERE project_id=? AND is_current=1"
        " AND rel='found_on'", (project,)).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM inbox_items WHERE project_id=? AND state='open'",
        (project,)).fetchone()[0] == 0, "undo 之后留下了待决 Inbox 项"

    after = _fingerprint(db)
    residue = sorted(t for t in after if after[t]["rows"] and after[t] != baseline[t])
    assert not residue, f"undo 之后还有残留行：{residue}"


def test_nuclei_sniff_separates_nuclei_from_httpx_jsonl():
    """两种来源**都是 JSONL**，而且 nuclei 的行也带 `host`。

    3.2 之前 httpx 的判据是"样本里有一半的行是带 host 的 JSON 对象"——
    nuclei 的文件会被它整份认领（0.95），于是仲裁拉不开差距、整批进人工裁决。
    所以这两条断言是一对：nuclei 认领自己，**httpx 不认领 nuclei**。
    """
    sample = _NUCLEI_GOLDEN.read_text(encoding="utf-8")
    assert nuclei.NucleiParser.sniff(sample, "scan.jsonl") >= 0.9
    assert httpx.HttpxParser.sniff(sample, "scan.jsonl") == 0.0

    # 反向：httpx 自己的文件不该被 nuclei 认领（判据是 template-id 而非"像 JSON"）。
    http_sample = (FIXTURES / "httpx" / "1" / "input.jsonl").read_text(encoding="utf-8")
    assert httpx.HttpxParser.sniff(http_sample, "httpx.jsonl") >= 0.9
    assert nuclei.NucleiParser.sniff(http_sample, "httpx.jsonl") == 0.0


def test_nuclei_severity_drift_is_reported_not_silently_clamped():
    """第七种 severity 是漂移信号：**如实保留并记名**，不是折成 unknown 抹平。

    与 nmap 对 `status.state` 的处理同一口径（`_HOST_STATES` 那一处）。
    """
    line = _finding(info={"name": "X", "severity": "catastrophic"})
    records, ctx = parse_with(nuclei.NucleiParser, [line])
    assert ctx.unknown_fields == {"info.severity=catastrophic": 1}, ctx.unknown_fields
    values = [r.value for r in records
              if isinstance(r, base.RawObservation) and r.type == "severity"]
    assert values == ["catastrophic"]


# ---------------------------------------------------------------------------
# 3.3 · **路径模板折叠**：一个是身份、三条是证据（本步的核心交付标准）
# ---------------------------------------------------------------------------
#
# 这一节是 3.3 最值得看的那几行。判据刻意**不是**"有没有产出 endpoint" ——
# 一个把每条 URL 都建成一个新实体的实现同样有产出，而且更简单。判据是：
#
#   * `/user/123`、`/user/456`、`/user/789` → 库里**只有一个** endpoint 实体，
#     但**三条** `path` 观测，且三条的原始路径逐条可见；
#   * `/user/abc` 与 `/user/123` **不**归并（不是所有段都是占位符）；
#   * `/api/v1/users` 与 `/api/v2/users` **不**归并（折掉版本号等于把"两代接口"
#     这件事抹掉）；
#   * 查询串进证据、不进身份；scheme 完全不进身份。
#
# 没有这四条，"折叠"就只是一个可以被"全都折成一个 {id}"冒充的说法 —— 而那种
# 实现是**静默丢信息**：接口镜头里两条不同的接口并成一条，谁也不会发现。

_ENDPOINT_FOLDING = "\n".join([
    "https://app.example.com/user/123",
    "https://app.example.com/user/456",
    "https://app.example.com/user/789",
    "https://app.example.com/user/abc",
    "http://app.example.com/api/v1/users",
    "http://app.example.com/api/v2/users",
]) + "\n"

def _write(tmp_path, name: str, text: str):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _obs_values(db, project_id: int, kind: str, key: str, field: str) -> list:
    return sorted(str(r[0]) for r in db.execute(
        "SELECT o.value FROM observations o"
        "  JOIN entities e ON e.id = o.entity_id"
        "  JOIN entity_keys ek ON ek.entity_id = o.entity_id"
        " WHERE o.project_id=? AND o.is_current=1 AND e.kind=? AND ek.entity_key=?"
        "   AND o.semantic_field=?", (project_id, kind, key, field)))


def test_endpoint_folding_merges_the_identity_and_keeps_every_raw_path(db, project,
                                                                      tmp_path):
    """**本步的核心交付标准**：三个 id 一个实体，三条原始路径逐条可见。

    ⚠️ 这两件事必须**同时**成立，只做到一件都不算：
    折了身份但没留证据 = 永久答不出"这个 endpoint 到底见过哪些真实路径"（3.5 的
    镜头页与 3.6 的体检都要用那份证据）；留了证据但没折身份 = 接口列表每抓一次
    就膨胀一轮，那正是折叠要解决的事。
    """
    report = ingest.ingest_file(
        db, options(db, project, tmp_path),
        _write(tmp_path, "gau.txt", _ENDPOINT_FOLDING))

    assert report["parser"] == "gau@1", report
    assert report["state"] == "done", report
    assert report["reconcile"]["ok"], report["reconcile"]

    keys = _keys(db, project, "endpoint")
    folded = "app.example.com/user/{id}"
    assert folded in keys, sorted(keys)
    # 三个不同的数字 id **没有**留下三个实体：`/user/` 底下只剩模板与那条字面量
    # 路由（`/user/abc` 是**故意**不折的，见下一条用例）。
    assert sorted(k for k in keys if k.startswith("app.example.com/user/")) == [
        "app.example.com/user/abc", folded], sorted(keys)

    assert _obs_values(db, project, "endpoint", folded, "path") == [
        "/user/123", "/user/456", "/user/789"]
    assert _obs_values(db, project, "endpoint", folded, "path_template") == [
        "/user/{id}"]

    # 接口挂在服务上，而那个服务与 httpx / nmap / nuclei 落的是**同一个**实体
    # （`httpx.service_key` 只在 httpx 里实现一次的意义）。
    assert "app.example.com:443" in _keys(db, project, "service")
    assert db.execute(
        "SELECT COUNT(*) FROM relations r"
        "  JOIN entity_keys s ON s.entity_id = r.src_id"
        "  JOIN entity_keys d ON d.entity_id = r.dst_id"
        " WHERE r.project_id=? AND r.is_current=1 AND r.rel='served_by'"
        "   AND s.entity_key=? AND d.entity_key='app.example.com:443'",
        (project, folded)).fetchone()[0] == 1


def test_endpoint_never_folds_across_versions_or_literal_segments(db, project,
                                                                  tmp_path):
    """两条**反例**，它们比正向那一条更重要。

    `/api/v1/users` 与 `/api/v2/users` 折在一起的话，"哪些还在用 v1"这个本工具
    存在的理由之一就永远答不出来了；`/user/abc` 折进 `/user/{id}` 则会把一条真实
    路由藏进模板里。两条都是**静默**的 —— 折叠少了只是行数多，折叠多了没人发现。
    """
    ingest.ingest_file(db, options(db, project, tmp_path),
                       _write(tmp_path, "gau.txt", _ENDPOINT_FOLDING))
    keys = _keys(db, project, "endpoint")

    assert "app.example.com/user/abc" in keys, sorted(keys)
    assert "app.example.com/user/{id}" in keys, sorted(keys)
    # `http://app.example.com/api/v1/users` 的 80 是**默认 web 端口**，不进身份。
    assert "app.example.com/api/v1/users" in keys, sorted(keys)
    assert "app.example.com/api/v2/users" in keys, sorted(keys)
    assert len([k for k in keys if "/api/v" in k]) == 2, sorted(keys)


def test_endpoint_query_string_is_evidence_not_identity(db, project, tmp_path):
    """`?id=1` **不算** key 的一部分（这是写下来的裁定，不是默认值）。

    查询串常带一次性 token，进身份会让同一个接口每抓一次就多一个实体。但原始串
    必须原样留在 `path` 观测里 —— 折叠的是身份，不是事实。
    """
    ingest.ingest_file(
        db, options(db, project, tmp_path),
        _write(tmp_path, "gau.txt",
               "https://app.example.com/admin\n"
               "https://app.example.com/admin?tab=users#list\n"
               "https://app.example.com/admin?id=1\n"))
    keys = _keys(db, project, "endpoint")
    assert keys == {"app.example.com/admin"}, sorted(keys)
    assert _obs_values(db, project, "endpoint", "app.example.com/admin",
                       "path") == ["/admin", "/admin?id=1", "/admin?tab=users#list"]


def test_endpoint_scheme_is_not_part_of_the_identity(db, project, tmp_path):
    """同一台机器上的同一个路径，换过协议还是**同一个接口**（v3.1 §4.4）。

    默认端口同理，而且这里有一处**必须写下来的坑**：折的不能是"按 scheme 取的
    默认端口"（`http→80` / `https→443`），否则 `http://a:80/x` 折成 `a/x` 而
    `https://a:80/x` 保留成 `a:80/x` —— scheme 又从后门进了身份。
    """
    ingest.ingest_file(
        db, options(db, project, tmp_path),
        _write(tmp_path, "gau.txt",
               "http://app.example.com/x\n"
               "https://app.example.com/x\n"
               "http://app.example.com:80/x\n"
               "https://app.example.com:443/x\n"
               "https://app.example.com:8443/x\n"))
    keys = _keys(db, project, "endpoint")
    assert keys == {"app.example.com/x", "app.example.com:8443/x"}, sorted(keys)
    # scheme 进了**证据**（观测），没进身份。
    assert _obs_values(db, project, "endpoint", "app.example.com/x",
                       "scheme") == ["http", "https"]
    # 端口 80 与 443 是**两个**默认 web 端口，与 scheme 无关 —— 所以 service
    # 有三个：80 / 443 / 8443。
    assert _keys(db, project, "service") == {
        "app.example.com:80", "app.example.com:443", "app.example.com:8443"}


def test_endpoint_relative_path_is_resolved_against_the_report_target(db, project,
                                                                      tmp_path):
    """dirsearch 给的是**相对路径**，主机在 `Target:` 那一行上。

    `Target:` 缺失时那条路径是**坏行**，不是"猜一个主机" —— 猜错的代价是一整批
    接口挂到错误的主机上，而且不会有任何信号。
    """
    path = _write(tmp_path, "dirsearch.txt",
                  "Target: http://app.example.com:8080/\n"
                  "\n"
                  "[12:00:00] Starting:\n"
                  "[12:00:01] 200 -  123B  - /admin\n"
                  "[12:00:02] 403 -  456B  - /.git/\n")
    report = ingest.ingest_file(db, options(db, project, tmp_path), path)
    assert report["parser"] == "dirsearch@1", report
    assert _keys(db, project, "endpoint") == {
        "app.example.com:8080/admin", "app.example.com:8080/.git/"}
    assert _obs_values(db, project, "endpoint", "app.example.com:8080/.git/",
                       "status") == ["403"]

    # 没有 Target 的同一批结果：全是坏行，**零事实**。
    lines = ["[12:00:01] 200 -  123B  - /admin"]
    records, ctx = parse_with(dirsearch.DirsearchParser, lines)
    assert records == []
    assert len(ctx.bad_lines) == 1, ctx.bad_lines
    assert "base" in ctx.bad_lines[0]["reason"], ctx.bad_lines


def test_endpoint_method_is_recorded_only_when_the_source_states_it():
    """计划书 §4.3 的"无方法记 `UNKNOWN`"落在**渲染层**，不落成一条空观测。

    写一条 `method=UNKNOWN` 是零信息噪声（它会实打实地抬高观测数与存储），
    与 httpx 对 `failed=False` 的口径一致 —— 缺观测本来就表达了"这个来源不产出
    方法"。反过来，katana / ffuf **给了**方法就必须记下来，而且要归一化
    （`get` / `Get` / `GET` 是同一个方法，不折的话接口镜头里会出现假的冲突）。
    """
    lines = [
        json.dumps({"request": {"method": "get",
                                "endpoint": "https://a.example.com/x"},
                    "response": {"status_code": 200}}),
        json.dumps({"request": {"method": "GET",
                                "endpoint": "https://a.example.com/y"},
                    "response": {"status_code": 200}}),
    ]
    records, _ = parse_with(katana.KatanaParser, lines)
    methods = [r.value for r in records
               if isinstance(r, base.RawObservation) and r.type == "method"]
    assert methods == ["GET", "GET"], methods

    # gau 不产出方法 → **一条 method 观测都没有**（不是 `UNKNOWN` 那种占位）。
    records, _ = parse_with(gau.GauParser, ["https://a.example.com/x"])
    assert not [r for r in records
                if isinstance(r, base.RawObservation) and r.type == "method"]


def test_endpoint_reimport_of_the_same_facts_adds_no_rows(db, project, tmp_path):
    """同一批事实换一份字节 → 一行新的都不该出现。

    这一条比"同一份文件导两遍"强：后者会走 sha256 幂等短路，**不足以**证明事实层
    的去重。这里字节真的变了（多一行注释），去重只能靠事实键。
    """
    ingest_opts = options(db, project, tmp_path)
    first = ingest.ingest_file(db, ingest_opts,
                               _write(tmp_path, "gau.txt", _ENDPOINT_FOLDING))
    assert first["counts"]["added"] > 0, first["counts"]
    rows_before = _row_counts(db, project)

    again = ingest.ingest_file(
        db, ingest_opts,
        _write(tmp_path, "gau-again.txt",
               _ENDPOINT_FOLDING
               + "# 多一行注释：字节变了，事实没变\n"))
    assert again["counts"]["records"] > 0, again["counts"]

    rows_after = _row_counts(db, project)
    drifted = {k: (rows_before[k], rows_after[k]) for k in rows_before
               if rows_before[k] != rows_after[k]}
    assert not drifted, f"重导（不同字节、同一批事实）造出了新行：{drifted}"


def test_endpoint_undo_removes_every_endpoint_fact(db, project, tmp_path):
    """`undo` 该 job → endpoint / service / served_by / 观测全部归零，不留 Inbox。

    "归零"用**逐行指纹**比而不是"某张表没有行"：撤销之后 `entities` 里的行还在
    （它是历史事实），只是 `is_current=0` —— 指纹比的是有效状态。
    """
    baseline = _fingerprint(db)
    report = ingest.ingest_file(db, options(db, project, tmp_path),
                                _write(tmp_path, "gau.txt", _ENDPOINT_FOLDING))
    assert _keys(db, project, "endpoint"), "先得有东西可撤"

    ops.undo(db, job_id=report["job_id"], actor="test")

    assert _keys(db, project, "endpoint") == set()
    assert _keys(db, project, "service") == set()
    assert db.execute(
        "SELECT COUNT(*) FROM relations WHERE project_id=? AND is_current=1"
        " AND rel='served_by'", (project,)).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM observations WHERE project_id=? AND is_current=1"
        " AND semantic_field='path'", (project,)).fetchone()[0] == 0

    # ⚠️ 唯一一条 undo **撤不掉**的残渣，而且它有名字：折叠出来的 endpoint 在
    # `path` 字段上有多个值，而模型的冲突检测把"同实体同字段多值"一律记成
    # `value_conflict`（`v_field_conflicts` 没有"多值字段"这个概念）。
    # 这条 Inbox 项的主体是 project 而不是 job（`created_by_job_id` 为 NULL），
    # 所以 undo 碰不到它 —— 已单独报给发起人，见本节的
    # `test_endpoint_multi_path_landing_in_the_inbox_is_a_pre_existing_model_gap`。
    after = _fingerprint(db)
    residue = sorted(t for t in after if after[t]["rows"] and after[t] != baseline[t])
    assert residue == ["inbox_items"], f"undo 之后还有别的残留行：{residue}"
    items = [{"kind": str(r["kind"]), "state": str(r["state"])} for r in db.execute(
        "SELECT kind, state FROM inbox_items WHERE project_id=?", (project,))]
    assert items == [{"kind": "value_conflict", "state": "open"}], items


def test_endpoint_multi_path_landing_in_the_inbox_is_a_pre_existing_model_gap(
        db, project, tmp_path):
    """折叠出来的 endpoint 会撞上「一个字段只有一个真值」这条模型假设。

    这条用例**不是**在描述一个正确行为，而是把一处**既有**的模型缺口钉在测试里，
    让它是可见的、可以被人拍板的：

    * `/user/123`、`/user/456`、`/user/789` 折成一个 endpoint，三条原始路径落在
      **同一个** `path` 字段的**三个不同值**上 —— 而 `v_field_conflicts` 把
      "同实体同字段多值"一律记成冲突（它没有"多值字段"这个概念），于是导入一个
      折叠过的 endpoint 就会开一条 `value_conflict` Inbox 项；
    * 那条 Inbox 项的主体是 project、不是 job，所以 **undo 撤不掉它**；
    * 数据量一大，接口镜头里每一个被折过的 endpoint 都会开一条这样的项，
      而 3.6 的体检页口径是"清空 Inbox 即干净"。

    ⚠️ 这**不是** 3.3 引入的形状 —— httpx 对多值字段 `cname` 早就是同一个行为
    （两条 CNAME → 一条 `cname` 冲突），本用例的两条断言把它一起证明了。
    修它要动 `v_field_conflicts` 或 observations 的建模，属 001 的地界（R4 不改），
    所以 3.3 的选择是：**如实按验收标准落三条观测**，把缺口原样报出来。
    """
    report = ingest.ingest_file(db, options(db, project, tmp_path),
                                _write(tmp_path, "gau.txt", _ENDPOINT_FOLDING))
    assert report["counts"]["conflicts"] == 1, report["counts"]
    items = [json.loads(str(r["payload_json"])) for r in db.execute(
        "SELECT payload_json FROM inbox_items WHERE project_id=? AND state='open'",
        (project,))]
    assert items == [{"field": "path",
                      "values": ["/user/123", "/user/456", "/user/789"]}], items

    # 既有先例：httpx 的多值 `cname` 走的是**同一条**路（所以缺口是模型的，
    # 不是 3.3 的）。
    other = _write(tmp_path, "httpx.jsonl", json.dumps(
        {"host": "b.example.com", "port": "443", "scheme": "https",
         "status_code": 200, "cname": ["x.cdn.net", "y.cdn.net"]}))
    httpx_report = ingest.ingest_file(db, options(db, project, tmp_path), other)
    assert httpx_report["counts"]["conflicts"] == 1, httpx_report["counts"]


# ---------------------------------------------------------------------------
# 3.3 · 负向输入（每个来源一组：空文件 / 截断 / 二进制 / 非 UTF-8）
# ---------------------------------------------------------------------------

#: 每个来源一条"截断"载荷 —— 各自截在**自己格式最脆的那一处**：
#: 文本截在 scheme 上、JSONL 截在括号里、整份 JSON 直接断在半路。
ENDPOINT_TRUNCATED = [
    ("gau", "gau", b"https:/app.example.com/admin\n", "不是一条绝对 URL"),
    ("katana", "katana", b'{"request": {"method": "GET", "endpo', "JSON 解析失败"),
    ("dirsearch", "dirsearch", b"[12:00:01] 200 -  12", "没有路径"),
    ("ffuf", "ffuf",
     b'{"config": {"method": "GET"}, "results": [{"url": "http://a.example.com/x"',
     "JSON 解析失败"),
]

_ENDPOINT_PARSERS = {"gau": gau.GauParser, "katana": katana.KatanaParser,
                     "dirsearch": dirsearch.DirsearchParser,
                     "ffuf": ffuf.FfufParser}


@pytest.mark.parametrize("name,tool,payload,reason", ENDPOINT_TRUNCATED,
                         ids=[c[0] for c in ENDPOINT_TRUNCATED])
def test_endpoint_truncated_input_lands_nothing_and_says_why(name, tool, payload,
                                                             reason):
    """**解析层**的"一条事实都不落"：截断的行产出零记录 + 一句人话。

    判据三件一起看才完整：零记录、恰好一条坏行（报出来了）、没有 Traceback。
    """
    lines = payload.decode("utf-8", "replace").splitlines()
    records, ctx = parse_with(_ENDPOINT_PARSERS[tool], lines)
    assert records == [], f"{name}: 截断的输入产出了记录"
    assert len(ctx.bad_lines) == 1, ctx.bad_lines
    assert reason in ctx.bad_lines[0]["reason"], ctx.bad_lines[0]["reason"]
    assert "Traceback" not in ctx.bad_lines[0]["reason"]


#: 每个来源四种坏输入：空文件 / 截断 / 二进制（zip） / 非 UTF-8（GBK）。
#: 前三种在 ingest 的探测层就该被拦下（空文件、二进制魔数、编码），第四种是
#: **能解码**的 —— 它走到解析器，然后在解析器里变成坏行。四条路径都要走一遍。
_ENDPOINT_NEGATIVE = [
    (f"{name}-{label}", tool, payload, label)
    for name, tool, payload, _reason in ENDPOINT_TRUNCATED
    for label, payload in (("empty", b""), ("truncated", payload),
                           ("binary", b"PK\x03\x04" + b"\x00" * 64 + b"zip"),
                           ("gbk", "中文路径\n".encode("gbk")))
]


@pytest.mark.parametrize("name,tool,payload,label", _ENDPOINT_NEGATIVE,
                         ids=[c[0] for c in _ENDPOINT_NEGATIVE])
def test_endpoint_negative_inputs_never_crash(db, project, tmp_path, name, tool,
                                              payload, label):
    """走**完整的 ingest 管线**（不是只调解析器）：可读报告，不是 Python 堆栈。"""
    path = tmp_path / f"{name}.dat"
    path.write_bytes(payload)
    report = ingest.ingest_file(db, options(db, project, tmp_path), path)

    assert isinstance(report, dict), label
    assert report["state"] in {"done", "partial", "skipped", "failed",
                               "needs_confirmation", "cancelled"}, (label, report)
    assert "Traceback" not in json.dumps(report, ensure_ascii=False), label
    # 这四种输入**没有一种**能算出接口来 —— 一条事实都不该落。
    assert report["counts"]["records"] == 0, (name, report["counts"])
    for kind in ("endpoint", "service", "domain", "ip"):
        assert not _keys(db, project, kind), f"{name} 落了 {kind} 实体"


def test_endpoint_ffuf_report_without_config_is_not_adopted(db, project, tmp_path):
    """`results` 数组**不是** ffuf 的签名 —— `config` 才是那道分水岭。

    一份"顶层有 results 但没有 config"的 JSON 不是 ffuf 的报告，ffuf 的 sniff
    给它 0.0；别的解析器也不认领它，所以正确的结局是进人工裁决、零事实 ——
    而不是被硬猜成一份目录爆破结果。
    """
    path = _write(tmp_path, "not-ffuf.json",
                  json.dumps({"results": [{"url": "http://a.example.com/x",
                                           "status": 200}]}, ensure_ascii=False))
    assert ffuf.FfufParser.sniff(path.read_text(encoding="utf-8"), "x.json") == 0.0
    report = ingest.ingest_file(db, options(db, project, tmp_path), path)
    assert report["counts"]["records"] == 0, report["counts"]
    assert not _keys(db, project, "endpoint")
