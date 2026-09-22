"""4.3 · 离线指纹引擎：规则可版本化 / 可回归 / 可解释，且零出站。

判据（每一条对应 LARI-38 的一条交付标准，标题即判据原文的落点）：

  ① **规则可版本化**：`rules_digest()` 只吃规则文件的原始字节，经
     `FingerprintParser.definition_extra()` 混进 `ingest._definition_hash()`。
     钉在 `fixtures/fingerprint/1/expected.json` 的 `rules_digest` 就是那根
     钉 —— **改任何一条规则，本文件第一条用例先红**。这是阶段交付标准
     「规则改动必须让夹具失败一次」的机械形态（红→绿的实测记录在交付评论里）。

  ② **可解释**：每个命中答得出"哪条规则、依据什么"——观测值里带
     `{product, rule, where, evidence}`，且观测的 `source` = 规则名
     （贡献的 `sources.name` 就是它，数据层事实，不是展示层拼接）。

  ③ **命中经既有写入口落库**（R1）：`product` + `service --runs--> product`
     + observation，全部走 `ingest` → `_write_record`；多值声明生效（同服务
     三条规则命中**不**开假冲突 Inbox 项）。

  ④ **零出站**（R7）：静态红线之外，这里再上一道**行为**断言 —— 把 socket
     的连接面全部封死，整条导入照样跑完：引擎在运行时一个连接都没开。

  ⑤ **undo 干净回滚**：逐行指纹相等（`snapshot_effective` /
     `effective_equivalent`，与 2.6 的验收网同一把尺子）。

  ⑥ **缺 extras 是人话**（4.5 的口径提前在本格式上兑现）：`MissingExtrasError`
     含 extras 名与安装命令，**不是**裸 `ImportError`，**更不是**静默 0 命中
     （那会把环境问题伪装成"这台主机没有指纹"）；嗅探照常认领。

  ⑦ **坏规则是可读错误**：pattern 编译失败 / 缺字段 / 重名，报错带文件名与
     规则名 —— 不带堆栈、不静默跳过。

⚠️  **夹具的来历**：`fixtures/fingerprint/1/input.http` 是按 HTTP/1.1 单次
交换线格式现写的**样本**，不是真抓包（本机零出站，也没有真站点可抓）。它
证明的是"引擎与规则自洽"，`provenance` 字段把这句话写在夹具里。
"""

from __future__ import annotations

import json
import socket
import sqlite3
import sys
from pathlib import Path

import pytest

from magpie.core import ingest, ops, store
from magpie.core.parsers import _fingerprints, base, fingerprint

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CASE = FIXTURES / "fingerprint" / "1"
REPO_ROOT = Path(__file__).resolve().parents[3]

#: 黄金夹具的输入（与 `load_case` 同一份文本）。
GOLDEN_TEXT = (CASE / "input.http").read_text(encoding="utf-8")


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = store.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    store.init_db(conn)
    yield conn
    conn.close()


@pytest.fixture()
def project(db) -> int:
    return ops.create_project(db, "fingerprint-test", {"include": [], "exclude": []})


def options(project_id: int, data_dir: Path, **kw) -> ingest.IngestOptions:
    return ingest.IngestOptions(project_id=project_id, data_dir=data_dir,
                                actor="test", verify=True, **kw)


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def parse(text: str) -> tuple[list, base.OfflineContext]:
    ctx = base.OfflineContext()
    records = list(fingerprint.FingerprintParser().parse(text.split("\n"), ctx))
    return records, ctx


def rules_as_file(tmp_path: Path, body: str) -> Path:
    """把一段规则 YAML 写成一个临时规则目录，返回目录。"""
    directory = tmp_path / "rules"
    directory.mkdir(exist_ok=True)
    (directory / "r.yaml").write_text(body, encoding="utf-8", newline="\n")
    return directory


# ===========================================================================
# ① 规则可版本化：digest 钉进夹具，改规则必变
# ===========================================================================


def test_rules_digest_matches_the_pinned_fixture():
    """**回归夹具的那根钉**：钉在 expected.json 里的 digest 必须等于今天实算的。

    改任何一条 `fingerprints/*.yaml`（加规则、改 pattern、改文件名、删文件）
    → digest 变 → 本条先红。修复它的方式**不是**改断言，而是把新的实算值
    连同新规则的用例一起更新 —— 这就是"规则改动必须让夹具失败一次，再加新
    用例修到绿"的流程本身。
    """
    pinned = json.loads((CASE / "expected.json").read_text(encoding="utf-8"))
    assert pinned["rules_digest"] == _fingerprints.rules_digest(), (
        "规则集 digest 与夹具里钉的不一致 —— fingerprints/*.yaml 被改过了。"
        "先确认这次改动是有意的，再把 expected.json 的 rules_digest 更新成"
        "新的实算值，并给新规则补用例")


def test_rules_digest_changes_when_any_rule_file_changes(tmp_path):
    """digest 的**机制**证明：内容变 → 变；文件增删 → 变。（上一条若红了，
    这一条回答"新的红是不是真的来自规则变化"。）"""
    directory = rules_as_file(tmp_path, "rules:\n  - name: r-one\n"
                                        "    product: nginx\n"
                                        "    match: {target: body, pattern: 'a'}\n")
    baseline = _fingerprints.rules_digest(directory)
    assert baseline.startswith("sha256:")

    # 内容变（哪怕一个字节）
    (directory / "r.yaml").write_text(
        "rules:\n  - name: r-one\n    product: nginx\n"
        "    match: {target: body, pattern: 'b'}\n", encoding="utf-8")
    assert _fingerprints.rules_digest(directory) != baseline

    # 文件增（多一个规则文件）
    (directory / "extra.yaml").write_text(
        "rules:\n  - name: r-two\n    product: php\n"
        "    match: {target: body, pattern: 'c'}\n", encoding="utf-8")
    after_add = _fingerprints.rules_digest(directory)
    assert after_add != baseline

    # 文件名也进 digest（同内容换个文件名同样是规则集变了）
    (directory / "r.yaml").rename(directory / "renamed.yaml")
    assert _fingerprints.rules_digest(directory) != after_add


def test_rules_digest_is_line_ending_invariant(tmp_path):
    """换行**不该**进 digest —— 本机 `core.autocrlf=true`，同一份规则在不同
    检出里是 LF 还是 CRLF 由 git 配置决定。不归一的话，钉在夹具里的 digest
    会在每个新克隆里必然变红 —— 一条因为换行而红的断言训练人忽略红色。"""
    directory = rules_as_file(tmp_path, "")           # 先建目录
    body = ("rules:\n  - name: r-one\n    product: nginx\n"
            "    match: {target: body, pattern: a}\n")
    target = directory / "r.yaml"
    target.write_bytes(body.encode("utf-8"))
    lf_digest = _fingerprints.rules_digest(directory)
    target.write_bytes(body.replace("\n", "\r\n").encode("utf-8"))
    assert _fingerprints.rules_digest(directory) == lf_digest, \
        "CRLF 检出让 digest 变了 —— 克隆会必然红"
    # 反面：换行之外的任何内容改动仍然要变（归一不等于只看文件名）
    target.write_bytes(body.replace("pattern: a", "pattern: b").encode("utf-8"))
    assert _fingerprints.rules_digest(directory) != lf_digest


def test_definition_extra_is_mixed_into_the_definition_hash():
    """`_definition_hash` 对有 `definition_extra` 的类：**同一份类源码**、
    extra 的值变了 → hash 必变；没有该属性的类走原路径（其余 13 个解析器的
    hash 一个字节都不变）。"""
    import hashlib
    import inspect

    from magpie.core import ingest as _ingest

    class Probe:
        @classmethod
        def definition_extra(cls):
            return "rules:v1"

    # 混入机制：带 extra 的类，hash ≠ 纯源码 hash。
    with_extra = _ingest._definition_hash(Probe)
    assert with_extra != hashlib.sha256(
        inspect.getsource(Probe).encode("utf-8")).hexdigest(), \
        "definition_extra 没有被混进 hash —— 判据 1 走不通"

    # 隔离证明：**类源码一个字节不变**（getsource 读的是文件文本），只换 extra
    # 的返回值 → hash 必变。类名、方法名、文件位置全都不动，变的只有 extra。
    Probe.definition_extra = classmethod(lambda cls: "rules:v2")
    assert _ingest._definition_hash(Probe) != with_extra, \
        "definition_extra 的值变了但 hash 没变 —— 改规则不会被看见"

    # 没有该属性：hash 必须与"纯源码 hash"逐字一致（行为零变化）。
    class Plain:
        x = 1

    assert _ingest._definition_hash(Plain) == hashlib.sha256(
        inspect.getsource(Plain).encode("utf-8")).hexdigest()

    # FingerprintParser 的 extra 就是规则 digest；夹具钉的值必须出现在里面。
    extra = fingerprint.FingerprintParser.definition_extra()
    pinned = json.loads((CASE / "expected.json").read_text(encoding="utf-8"))
    assert extra == "rules:" + pinned["rules_digest"], extra


def test_rule_names_never_collide_with_parser_tool_names():
    """规则名会成为 `sources.name` —— 与解析器工具同名会让 source 目录把两者
    折叠成一行（观测被记到别人的 source 头上）。这条在测试里查而不是在产品
    代码里维护一份工具名清单：清单会分叉，发现机制不会。"""
    parsers, _source = ingest.load_parsers()
    tools = {p.tool for p in parsers}
    collisions = [r.name for r in _fingerprints.load_rules() if r.name in tools]
    assert not collisions, f"规则名与解析器工具名撞车：{collisions}"


def test_a_new_rule_hits_once_its_pin_is_updated():
    """**回归流程的"修绿"那一半**：改规则让夹具红了之后，正确的修复是
    ①更新钉住的 digest、②给新规则补用例 —— 本条就是 ②。

    例证：`tomcat-server` 这条规则是按交付标准的流程加进来的（先跑一遍看
    钉红了，再更新 `rules_digest` 并补上这条用例），它今天必须命中：
    换掉的 pattern、忘补的用例，都会在这里当场红。
    """
    rules = {r.name: r for r in _fingerprints.load_rules()}
    assert "tomcat-server" in rules, "新规则不见了 —— 它是怎么进来的就该怎么在"

    sample = ("GET https://app.example.com/manager HTTP/1.1\n"
              "Host: app.example.com\n\n"
              "HTTP/1.1 200 OK\nServer: Apache Tomcat/9.0.85\n\n<html></html>")
    records, ctx = parse(sample)
    assert not ctx.bad_lines, ctx.bad_lines
    observations = [r for r in records if isinstance(r, base.RawObservation)]
    assert len(observations) == 1, observations
    obs = observations[0]
    assert obs.value["rule"] == "tomcat-server"
    assert obs.value["product"] == "tomcat@9.0.85", obs.value
    assert obs.source == "tomcat-server"
    products = [r.key for r in records
                if isinstance(r, base.RawEntity) and r.kind == "product"]
    assert products == ["tomcat@9.0.85"], products


def test_every_shipped_rule_loads_and_normalizes():
    """出厂规则逐条可加载：名字形状、pattern 可编译、product 过别名表 ——
    `load_rules` 的校验就是断言本身（坏的当场抛 ValueError）。"""
    rules = _fingerprints.load_rules()
    names = [r.name for r in rules]
    assert len(names) == len(set(names)), f"规则名重复：{names}"
    assert {"nginx-server", "apache-server", "iis-server",
            "php-x-powered-by", "wp-content-body"} <= set(names), names
    by_name = {r.name: r for r in rules}
    assert by_name["apache-server"].product == "httpd", (
        "Apache httpd 必须被别名表折成 httpd —— 否则 nmap/httpx 与指纹引擎会"
        "为同一个产品落两个实体")
    assert by_name["nginx-server"].version_from == 1
    assert by_name["wp-content-body"].version_from == 0, \
        "wp-content 不含版本信息 —— 编一个版本出来就是假事实"


# ===========================================================================
# ⑦ 坏规则 = 可读错误（不是堆栈，不是静默跳过）
# ===========================================================================


@pytest.mark.parametrize("body, expect", [
    ("rules:\n  - name: bad name!\n    product: nginx\n"
     "    match: {target: body, pattern: 'a'}\n", "name 不合法"),
    ("rules:\n  - name: ok-rule\n    product: nginx\n"
     "    match: {target: body, pattern: '('}\n", "不是合法正则"),
    ("rules:\n  - name: ok-rule\n    match: {target: body, pattern: 'a'}\n",
     "缺 `product`"),
    ("rules:\n  - name: ok-rule\n    product: nginx\n"
     "    match: {target: body, pattern: '(a)'}\n    version_from: 5\n",
     "捕获组"),
    ("rules:\n  - name: ok-rule\n    product: nginx\n"
     "    match: {target: cookie, pattern: 'a'}\n", "match.target"),
    ("not-a-mapping\n", "顶层必须是带 `rules:` 列表的对象"),
    ("rules: []\n", "空的"),
])
def test_bad_rule_is_a_readable_error(tmp_path, body, expect):
    """坏规则在**加载时**就报错：消息带文件名（与规则名），不带堆栈。

    静默跳过一条坏规则是最坏的下场 —— 它会把"规则写错了"伪装成"这台主机
    没有指纹"，而两个方向的错都不会有第二个信号。
    """
    directory = rules_as_file(tmp_path, body)
    with pytest.raises(ValueError) as exc:
        _fingerprints.load_rules(directory)
    message = str(exc.value)
    assert expect in message, message
    assert "r.yaml" in message, message


def test_missing_rule_directory_is_a_readable_error(tmp_path):
    """规则目录没了 = 仓库内容缺失 —— 报可读错误，不是 0 条规则的静默空转。"""
    with pytest.raises(ValueError) as exc:
        _fingerprints.load_rules(tmp_path / "nope")
    assert "没有任何 *.yaml" in str(exc.value)


# ===========================================================================
# ⑥ 缺 yaml extras = 人话（照 3.4 OpenAPI 的模板）
# ===========================================================================


def test_missing_yaml_extras_is_a_readable_error(monkeypatch):
    """三条断言缺一不可（与 `test_openapi_yaml_without_the_extras_*` 同口径）：

    1. **不是裸 `ImportError`** —— 那把人引去看 `sys.path`；
    2. 消息含 extras 名与安装命令；
    3. **sniff 照常认领**（0.95）—— 少了它，输入会掉进人工裁决，环境问题被
       伪装成格式问题（3.4 点名过的头号陷阱）。
    """
    monkeypatch.setitem(sys.modules, "yaml", None)

    assert fingerprint.FingerprintParser.sniff(GOLDEN_TEXT, "x.http") == 0.95

    ctx = base.OfflineContext()
    with pytest.raises(base.MissingExtrasError) as exc:
        list(fingerprint.FingerprintParser().parse(GOLDEN_TEXT.split("\n"), ctx))
    message = str(exc.value)
    assert not isinstance(exc.value, ImportError)
    assert "api-yaml" in message and "pip install" in message, message
    # 关键的反面：**不许**静默降级成"零命中"。
    assert "nginx" in message or "规则" in message, message


def test_missing_yaml_extras_reaches_the_report_readably(db, project, tmp_path):
    """端到端：extras 缺失时导入给出**可读报告**，不是堆栈、不是 0 记录的成功。"""
    path = write(tmp_path, "resp.http", GOLDEN_TEXT)
    saved = sys.modules.get("yaml", None)
    had = "yaml" in sys.modules
    sys.modules["yaml"] = None
    try:
        report = ingest.ingest_file(db, options(project, tmp_path), path)
    finally:
        if had:
            sys.modules["yaml"] = saved
        else:
            sys.modules.pop("yaml", None)

    assert report["parser"] == "fingerprint@1", report
    assert report["state"] == "partial", report
    assert "MissingExtrasError" in report["parse_error"], report["parse_error"]
    assert "pip install" in report["parse_error"], report["parse_error"]
    assert report["counts"]["records"] == 0


# ===========================================================================
# ② 可解释：哪条规则、依据什么；source = 规则名
# ===========================================================================


def test_every_hit_answers_which_rule_and_on_what_evidence():
    """每个命中都答得出"哪条规则、依据什么"（§4.3 的原话）。"""
    records, ctx = parse(GOLDEN_TEXT)
    assert not ctx.bad_lines, ctx.bad_lines
    observations = [r for r in records if isinstance(r, base.RawObservation)]
    assert len(observations) == 3, observations
    for obs in observations:
        assert obs.type == "fingerprint"
        assert obs.source == obs.value["rule"], (
            "观测的 source 必须等于它自己的规则名 —— 两者分叉就答不出"
            "「这条观测是哪条规则写的」")
        assert obs.value["rule"] and obs.value["evidence"] and obs.value["where"]
        assert obs.value["product"]
    rules_seen = {obs.value["rule"] for obs in observations}
    assert rules_seen == {"nginx-server", "php-x-powered-by", "wp-content-body"}
    nginx = next(o for o in observations if o.value["rule"] == "nginx-server")
    assert nginx.value["where"] == "header:Server"
    assert "nginx/1.24.0" in nginx.value["evidence"]


def test_landing_sources_carry_the_rule_name(db, project, tmp_path):
    """**数据层**的可解释：观测的贡献挂在 `sources.name = 规则名` 的来源上
    （不是导入后拼出来的字符串）。"""
    path = write(tmp_path, "resp.http", GOLDEN_TEXT)
    report = ingest.ingest_file(db, options(project, tmp_path), path)
    assert report["state"] == "done", report

    rows = db.execute("""
        SELECT o.value, s.name AS source_name, s.tool
          FROM observations o
          JOIN v_active_contributions c
            ON c.subject_kind = 'observation' AND c.subject_id = o.id
          JOIN sources s ON s.id = c.source_id
         WHERE o.semantic_field = 'fingerprint'
         ORDER BY s.name""").fetchall()
    got = [(dict(r)["source_name"], dict(r)["tool"]) for r in rows]
    assert got == [("nginx-server", "fingerprint"),
                   ("php-x-powered-by", "fingerprint"),
                   ("wp-content-body", "fingerprint")], got


# ===========================================================================
# ③ 命中写 product + runs + observation，经既有写入口（R1）
# ===========================================================================


def test_hits_land_product_runs_and_observation_through_ingest(db, project,
                                                               tmp_path):
    """完整管线：`product` 实体 + `service --runs--> product` + 观测，
    多值声明生效（三条观测同实体同字段**不开**假冲突），且 undo/redo 可逆。"""
    path = write(tmp_path, "resp.http", GOLDEN_TEXT)
    report = ingest.ingest_file(db, options(project, tmp_path), path)
    assert report["state"] == "done", report
    assert report["counts"]["records"] == 10, report["counts"]
    assert report["counts"]["conflicts"] == 0, \
        "fingerprint 字段已声明多值 —— 同服务三条命中不该开冲突"

    products = {r["entity_key"] for r in db.execute(
        "SELECT ek.entity_key FROM entities e JOIN entity_keys ek"
        " ON ek.entity_id = e.id"
        " WHERE e.is_current = 1 AND e.kind = 'product'")}
    assert products == {"nginx@1.24.0", "php@8.2.12", "wordpress"}, products

    runs = db.execute("""
        SELECT sa.alias_key AS src_key, da.alias_key AS dst_key
          FROM relations r
          JOIN v_active_contributions c
            ON c.subject_kind = 'relation' AND c.subject_id = r.id
          JOIN entity_alias sa ON sa.entity_id = r.src_id
          JOIN entity_alias da ON da.entity_id = r.dst_id
         WHERE r.rel = 'runs'""").fetchall()
    got = {(dict(r)["src_key"], dict(r)["dst_key"]) for r in runs}
    assert got == {("app.example.com:443", "nginx@1.24.0"),
                   ("app.example.com:443", "php@8.2.12"),
                   ("app.example.com:443", "wordpress")}, got

    # 写入口唯一（R1）：一条 Inbox 冲突都没有 —— 三个值落成多值而不是三个
    # 假冲突，说明它们走的是同一条带声明的路。
    assert db.execute("SELECT COUNT(*) FROM inbox_items").fetchone()[0] == 0


def test_same_dump_on_two_hosts_converges_on_the_same_product(db, project,
                                                              tmp_path):
    """同版本产品的跨服务收敛：两个服务都跑 nginx/1.24.0 → **一个** product
    实体、两条 runs（product_key 的身份裁定：产品名@版本，与服务无关）。"""
    for i, host in enumerate(("a.example.com", "b.example.com"), start=1):
        write(tmp_path, f"resp{i}.http",
              f"GET https://{host}/ HTTP/1.1\nHost: {host}\n\n"
              f"HTTP/1.1 200 OK\nServer: nginx/1.24.0\n\n<html></html>\n")
    for name in ("resp1.http", "resp2.http"):
        report = ingest.ingest_file(db, options(project, tmp_path),
                                    tmp_path / name)
        assert report["state"] == "done", report

    products = [r["entity_key"] for r in db.execute(
        "SELECT ek.entity_key FROM entities e JOIN entity_keys ek"
        " ON ek.entity_id = e.id"
        " WHERE e.is_current = 1 AND e.kind = 'product'")]
    assert products == ["nginx@1.24.0"], products
    runs = db.execute(
        "SELECT COUNT(*) FROM relations WHERE rel = 'runs'"
        " AND is_current = 1").fetchone()[0]
    assert runs == 2, runs


def test_engine_handles_origin_form_with_explicit_port():
    """origin-form + Host 带端口：不猜、不拒 —— 端口进 service 身份，
    443 → https、8080 → http（`_implicit_scheme` 的裁定）。"""
    body = ("GET /admin HTTP/1.1\nHost: app.example.com:8080\n\n"
            "HTTP/1.1 200 OK\nServer: nginx/1.24.0\n\n<html></html>")
    records, ctx = parse(body)
    assert not ctx.bad_lines, ctx.bad_lines
    service = next(r for r in records if isinstance(r, base.RawEntity)
                   and r.kind == "service")
    assert service.key == "app.example.com:8080", service.key


@pytest.mark.parametrize("text, keyword", [
    # 只有响应、没有请求段 → 指不到服务，如实拒绝（flow 同款纪律）。
    ("HTTP/1.1 200 OK\nServer: nginx/1.24.0\n\n<html></html>",
     "没有请求段"),
    # origin-form + Host 无端口 → 80/443 歧义，拒绝而不是猜。
    ("GET / HTTP/1.1\nHost: app.example.com\n\n"
     "HTTP/1.1 200 OK\nServer: nginx/1.24.0\n\n<html></html>",
     "不带端口"),
    # 连请求行都不是 → 格式不对，如实说。
    ("hello world\n", "既不是请求行"),
    # 响应段缺失（请求段后没有空行分隔）。
    ("GET https://a.example.com/ HTTP/1.1\nHost: a.example.com",
     "响应段缺失"),
])
def test_structural_mismatches_are_readable_rejections(text, keyword):
    """四类结构问题各一条：**零记录 + 一句人话**（负向夹具的判据形状）。"""
    records, ctx = parse(text)
    assert records == [], records
    assert len(ctx.bad_lines) == 1, ctx.bad_lines
    assert keyword in ctx.bad_lines[0]["reason"], ctx.bad_lines
    assert "Traceback" not in ctx.bad_lines[0]["reason"]


# ===========================================================================
# ④ 零出站：静态红线之外的行为断言
# ===========================================================================


def test_engine_never_opens_a_socket(db, project, tmp_path, monkeypatch):
    """整条导入跑完，而 socket 的每一个连接面都会**炸** —— 引擎在运行时
    一个连接都没开（R7 是静态扫描，这一条是行为证据）。"""
    def _explode(*_args, **_kwargs):
        raise AssertionError("指纹引擎打开了一个连接 —— R7 零出站被破坏")

    monkeypatch.setattr(socket, "socket", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)
    monkeypatch.setattr(socket, "socketpair", _explode)

    path = write(tmp_path, "resp.http", GOLDEN_TEXT)
    report = ingest.ingest_file(db, options(project, tmp_path), path)
    assert report["state"] == "done", report
    assert report["counts"]["records"] == 10, report["counts"]


def test_engine_imports_no_outbound_clients():
    """静态面：引擎与解析器两个模块的顶层 import 里没有出站客户端、没有
    socket（R7 扫全仓，这里把"新代码在射程内"钉成自己的断言）。"""
    import ast
    for module in (_fingerprints, fingerprint):
        path = Path(module.__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in {
                        "httpx", "requests", "urllib3", "socket"}, alias.name
                    assert not alias.name.startswith("urllib"), alias.name
            elif isinstance(node, ast.ImportFrom) and node.module \
                    and not node.level:
                root = node.module.split(".")[0]
                assert root not in {"httpx", "requests", "urllib3", "socket",
                                    "urllib"}, node.module


# ===========================================================================
# ⑤ undo 干净回滚（逐行指纹相等）
# ===========================================================================


def test_undo_is_clean_and_redo_reproduces_exactly(db, project, tmp_path):
    """导入 → undo → 逐行指纹回到导入前；redo → 逐行指纹精确复原。

    与 2.6 验收网（`verify_v32r5`）同一把尺子：`snapshot_effective` 比的是
    每张表的**每一行**，不是条数 —— 条数相同、内容不同它抓得住。
    """
    path = write(tmp_path, "resp.http", GOLDEN_TEXT)
    before = store.snapshot_effective(db)

    report = ingest.ingest_file(db, options(project, tmp_path), path)
    assert report["state"] == "done"
    after_import = store.snapshot_effective(db)
    assert after_import != before, "导入没落下任何事实 —— 后面的比较无意义"

    ops.undo(db, int(report["job_id"]), actor="test")
    same, diff = store.effective_equivalent(db, before)
    assert same, f"undo 没有干净回滚：{diff}"

    ops.redo(db, int(report["job_id"]), actor="test")
    same, diff = store.effective_equivalent(db, after_import)
    assert same, f"redo 没有精确复原：{diff}"


def test_reimport_is_idempotent(db, project, tmp_path):
    """同一份 dump 重导 → 幂等跳过（同一 sha256 + 同一 fingerprint），一行未写。"""
    path = write(tmp_path, "resp.http", GOLDEN_TEXT)
    first = ingest.ingest_file(db, options(project, tmp_path), path)
    assert first["state"] == "done"
    snapshot = store.snapshot_effective(db)

    second = ingest.ingest_file(db, options(project, tmp_path), path)
    assert second["state"] == "skipped", second
    same, diff = store.effective_equivalent(db, snapshot)
    assert same, f"幂等跳过却动了库：{diff}"


# ===========================================================================
# 嗅探：认领自己的格式，不抢别人的
# ===========================================================================


def test_sniff_claims_exchange_dumps_and_rejects_everything_else():
    """自己的格式 0.95；别人的样本（JSON / 清单 / XML / curl 命令 / 散文）
    一律 0 —— 抢错文件的下场是整批输入被解析成坏行。"""
    assert fingerprint.FingerprintParser.sniff(GOLDEN_TEXT, "x.http") == 0.95
    assert fingerprint.FingerprintParser.sniff("", "x") == 0.0
    # 纯响应（没有请求行/状态行打头的其它格式）
    samples = {
        "httpx jsonl": '{"host": "a.example.com", "status_code": 200}\n',
        "domainlist": "a.example.com\nb.example.com\n",
        "nmap xml": '<?xml version="1.0"?><nmaprun></nmaprun>',
        "curl": "curl https://a.example.com/\ncurl https://b.example.com/\n",
        "har json": '{"log": {"entries": []}}',
        "prose": "这是一段中文，里面提到 HTTP/1.1 200 也不是响应。\n",
    }
    for label, sample in samples.items():
        assert fingerprint.FingerprintParser.sniff(sample, f"{label}.txt") == 0.0, \
            f"抢了 {label} 的文件"
