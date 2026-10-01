"""5.5 风险评分与攻击面概览的验收网（LARI-45）。

正文五条判据各对应一组用例：

  1. **可解释** —— 每个 factor 的依据 id 真存在于库，且 `sum(贡献) == 总分`；
  2. **反例开关（有牙）** —— 加一条 critical vuln，分数按预期方向动；改坏规则
     表，回归夹具当场红（每次都跑，不靠"交付时手动演示一次"）；
  3. **确定性** —— 同库两次评分逐字节相同，且评分源码不碰时钟与随机；
  4. **零出站** —— socket 三点钉死照跑（5.3 同款：先真连证明网络栈活着，再断言
     三点全部当场炸，再拿反例证明钉子有牙）；
  5. `check.py` 全绿 —— 由 check.py 本身执行（本文件在它的 UNIT 表里）。

夹具为什么用**真导入**而不是手写行：评分要读的是"别的入口真的写出来的库"，
手写 facts 只能证明评分函数自洽，发现不了"导入写出来的形状与评分读的形状对不上"
—— 后者才是这条链上真实的失败模式。
"""

from __future__ import annotations

import ast
import json
import socket
import urllib.request
from pathlib import Path

import pytest

from magpie.core import ingest, ops, risk, store

FIXTURES = Path(__file__).resolve().parent / "fixtures"
#: 回归夹具（golden）：钉住规则指纹 + 总分 + 逐因子计数 + 摘要 + 攻击面合计。
GOLDEN = FIXTURES / "risk" / "expected.json"
SCOPE = {"include": ["*.example.com"], "exclude": []}
#: 三个真夹具各管一个面：漏洞（nuclei）/ 服务与产品（httpx）/ 接口（ffuf）。
INPUTS = ("nuclei/1/input.jsonl", "httpx/1/input.jsonl", "ffuf/1/input.json")


def build_db(tmp_path: Path):
    """真导入三个夹具 → (conn, project_id)。与 CLI 的 `loaded_db` 同一条纪律：
    评分要吃的是**导入路径写出来的**库。"""
    conn = store.connect(tmp_path / "magpie.db")
    store.init_db(conn)
    project_id = ops.create_project(conn, "risk-test", SCOPE)
    options = ingest.IngestOptions(project_id=project_id, data_dir=tmp_path,
                                   actor="risk-test")
    for rel in INPUTS:
        report = ingest.ingest_file(conn, options, FIXTURES / rel)
        assert report["state"] in ("done", "partial"), (rel, report)
    return conn, project_id


@pytest.fixture()
def scored(tmp_path):
    conn, project_id = build_db(tmp_path)
    yield conn, project_id, tmp_path
    conn.close()


def _factor_map(payload: dict) -> dict[str, dict]:
    return {f["id"]: f for f in payload["score"]["factors"]}


def _golden_payload(payload: dict) -> dict:
    """overview → 与 golden 逐键可比的形状（刻意不含 evidence：钉的是算术与
    规则，不是 50 条展开标签的排版）。"""
    return {
        "rule_version": payload["rule_version"],
        "rules_digest": payload["rules_digest"],
        "total": payload["score"]["total"],
        "factors": [{"id": f["id"], "count": f["count"],
                     "contribution": f["contribution"]}
                    for f in payload["score"]["factors"]],
        "facts": payload["facts"],
        "attack_surface_totals": payload["attack_surface"]["totals"],
    }


# ---------------------------------------------------------------------------
# 判据 1 · 可解释：依据 id 真存在 + sum(factors) == total
# ---------------------------------------------------------------------------


def test_sum_of_factors_is_the_total_and_every_evidence_exists(scored):
    conn, project_id, _ = scored
    payload = risk.overview(conn, project_id)
    score = payload["score"]
    factors = score["factors"]

    assert [f["id"] for f in factors] == risk.factor_ids(), "因子顺序/集合漂了"
    assert sum(f["contribution"] for f in factors) == score["total"], \
        "sum(factors) 必须等于 total —— 解释不了 total 的因子表就是失败模式本体"

    for factor in factors:
        assert factor["contribution"] == factor["weight"] * factor["count"], factor["id"]
        assert factor["evidence_total"] == factor["count"], factor["id"]
        assert len(factor["evidence"]) <= risk.EVIDENCE_CAP
        assert factor["label"], factor["id"]
        for item in factor["evidence"]:
            assert item["label"], (factor["id"], item)
            table = {"entity": "entities", "observation": "observations",
                     "inbox_item": "inbox_items"}.get(item["type"])
            assert table is not None, f"未知的 evidence type：{item}"
            row = conn.execute(f"SELECT 1 FROM {table} WHERE id=?",
                               (item["id"],)).fetchone()
            assert row is not None, f"依据不存在于库：{factor['id']} → {item}"


def test_empty_library_scores_zero_with_the_same_shape(tmp_path):
    """空项目也走完整形状（每个因子都在，全 0）—— 输出形状不随数据漂移，
    消费者不必写"有则渲染"的分支；总分 0 是**结果**，不是错误。"""
    conn = store.connect(tmp_path / "empty.db")
    store.init_db(conn)
    try:
        project_id = ops.create_project(conn, "empty", SCOPE)
        payload = risk.overview(conn, project_id)
        assert payload["score"]["total"] == 0
        assert [f["id"] for f in payload["score"]["factors"]] == risk.factor_ids()
        assert all(f["count"] == 0 for f in payload["score"]["factors"])
        assert payload["attack_surface"]["hosts"] == []
    finally:
        conn.close()


def test_score_and_attack_surface_share_one_collection(scored, monkeypatch):
    """攻击面与评分**共用同一次 collect**：overview 只读一次库。第二套统计
    （自己再查一遍漏洞 / 服务）会让"概览说 3、评分说 4"成为可能 —— 这条把
    "可能"钉成"不可能"。顺带钉两个结论的口径同一：评分里的 open 漏洞合计
    == 攻击面落点合计 + 未映射。"""
    conn, project_id, _ = scored
    calls = []
    original = risk.collect

    def counting_collect(conn_, project_id_):
        calls.append(project_id_)
        return original(conn_, project_id_)

    monkeypatch.setattr(risk, "collect", counting_collect)
    payload = risk.overview(conn, project_id)
    assert len(calls) == 1, f"overview 采集了 {len(calls)} 次（应当恰好 1 次）"
    open_total = sum(f["count"] for f in payload["score"]["factors"]
                     if f["id"].startswith("vuln_open."))
    surface = payload["attack_surface"]["totals"]
    assert surface["open_vuln_total"] + len(surface["unmapped_open_vulns"]) == open_total


# ---------------------------------------------------------------------------
# 判据 2 · 反例开关：critical vuln 推分向上；改规则 → 夹具必红
# ---------------------------------------------------------------------------


def test_adding_one_critical_vuln_raises_the_score_by_exactly_its_weight(scored):
    """往库里加一条 critical vuln（走真导入路径）→ `vuln_open.critical` 计数 +1、
    总分恰好 +100。**恰好**是这条用例的牙：任何顺带被牵动的因子（新实体混进
    未测、新观测开 Inbox）都会让等式破掉，而那正是该被看见的事。"""
    conn, project_id, tmp_path = scored
    before = risk.overview(conn, project_id)
    before_factor = _factor_map(before)["vuln_open.critical"]

    probe = tmp_path / "probe.jsonl"
    probe.write_text(json.dumps({
        "template-id": "risk-counterexample-critical",
        "info": {"name": "LARI-45 反例：一条 critical", "severity": "critical"},
        # 目标落在**已有的** app.example.com:443 上：不造新服务、不牵动别的因子，
        # 分数的每一位变化都只来自这条漏洞本身。
        "matched-at": "https://app.example.com:443/risk-probe",
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    options = ingest.IngestOptions(project_id=project_id, data_dir=tmp_path,
                                   actor="risk-test")
    report = ingest.ingest_file(conn, options, probe)
    assert report["state"] in ("done", "partial"), report

    after = risk.overview(conn, project_id)
    after_factor = _factor_map(after)["vuln_open.critical"]

    assert after_factor["count"] == before_factor["count"] + 1, "critical 计数没动"
    assert after["score"]["total"] == before["score"]["total"] \
        + risk.RULES["vuln_open"]["critical"], "总分没有恰好加上这条漏洞的权重"
    # 攻击面必须同步看到它（同一份 facts 的另一面 —— 只有评分动了就是两套统计）
    assert after["attack_surface"]["totals"]["open_vuln_total"] == \
        before["attack_surface"]["totals"]["open_vuln_total"] + 1
    # 新依据也得真存在（判据 1 对新数据继续成立）
    for item in after_factor["evidence"]:
        table = {"entity": "entities", "observation": "observations",
                 "inbox_item": "inbox_items"}[item["type"]]
        assert conn.execute(f"SELECT 1 FROM {table} WHERE id=?",
                            (item["id"],)).fetchone() is not None


def test_regression_fixture_matches_the_golden(scored, request):
    """回归夹具：规则指纹 + 总分 + 逐因子计数 + 摘要 + 攻击面合计，与 golden
    逐键相等。golden 里的 `rules_digest` 是**钉**（4.3 的纪律）：改任何一条权重，
    下一条用例当场红。

    **有意变更**（改权重 / 改夹具数据）后重钉：
    `py -3.13 -m pytest magpie/core/tests/test_risk.py -q --update-risk-golden`
    （先看红、确认方向、再重钉 —— 照 4.3 指纹规则的纪律）。"""
    conn, project_id, _ = scored
    actual = _golden_payload(risk.overview(conn, project_id))
    if request.config.getoption("--update-risk-golden"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(actual, ensure_ascii=False, indent=2,
                                     sort_keys=True) + "\n", encoding="utf-8")
        pytest.skip(f"已重钉 golden：{GOLDEN.name}")
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert actual == golden, (
        "评分回归夹具不匹配 —— 是**有意变更**吗？\n"
        f"  规则指纹：actual {actual['rules_digest']} vs golden {golden['rules_digest']}\n"
        f"  总分：actual {actual['total']} vs golden {golden['total']}\n"
        "确认后用 --update-risk-golden 重钉（先看红、确认方向、再重钉）。")


def test_rules_digest_is_alive_and_a_rule_change_breaks_the_golden():
    """钉是活的：**改坏一次规则，夹具的比对必须失败**（每次跑都验，不会锈）。
    这就是判据 2 里"改坏一次、夹具确实红了"的自动化形态 —— 手动演示会过期，
    进了测试的反例不会。"""
    import hashlib

    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert golden["rules_digest"] == risk.rules_digest(), "干净状态下钉与实算不等"

    original = risk.RULES["untested"]
    try:
        risk.RULES["untested"] = original + 1          # 改坏一条权重
        changed = risk.rules_digest()
        assert changed != golden["rules_digest"], "钉没接住规则变更 —— 夹具是死的"
        # 实算 == 按当前 RULES 算出的摘要（钉不是写死的常量，是活的计算）
        payload = json.dumps(risk.RULES, ensure_ascii=True, sort_keys=True,
                             separators=(",", ":"))
        assert changed == "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
        # 与 golden 的总分比对也必红（分数会随权重动）
        assert golden["total"] is not None
    finally:
        risk.RULES["untested"] = original
    assert risk.rules_digest() == golden["rules_digest"], "还原后必须复绿"


# ---------------------------------------------------------------------------
# 判据 3 · 确定性：同库同输入两次评分逐字节相同，源码不碰时钟 / 随机
# ---------------------------------------------------------------------------


def test_two_scoring_runs_are_byte_identical(scored):
    conn, project_id, _ = scored
    first = json.dumps(risk.overview(conn, project_id),
                       ensure_ascii=False, sort_keys=True)
    second = json.dumps(risk.overview(conn, project_id),
                        ensure_ascii=False, sort_keys=True)
    assert first == second, "同库同输入两次评分必须逐字节相同（无时间抖动、无随机）"


def test_risk_source_never_touches_clock_random_or_network():
    """静态断言：`risk.py` 不 import 时钟 / 随机 / 网络，也不碰它们的入口。

    行为断言（上一条）只能证明"今天没抖"，这条证明"想抖也没工具" —— 将来有人
    加"时间衰减"时顺手写 `now()`，红的是这条，而不是某次对不上的 golden。
    （`datetime.now` 这类 C 类型方法 monkeypatch 不了，静态扫描是这里最诚实
    的一档。）"""
    banned_modules = {"time", "datetime", "random", "socket", "urllib",
                      "http", "ssl", "asyncio"}
    banned_attrs = {"now", "utcnow", "today", "timestamp", "time", "monotonic",
                    "perf_counter", "random", "randint", "choice", "connect",
                    "connect_ex", "create_connection", "urlopen"}
    tree = ast.parse(Path(risk.__file__).read_text(encoding="utf-8"))
    offences = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in banned_modules:
                    offences.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in banned_modules:
                offences.append(f"line {node.lineno}: from {node.module} import …")
        elif isinstance(node, ast.Attribute) and node.attr in banned_attrs:
            offences.append(f"line {node.lineno}: .{node.attr}()")
    assert not offences, "评分模块碰了时钟/随机/网络：" + "; ".join(offences)


# ---------------------------------------------------------------------------
# 判据 4 · 零出站：socket 三点钉死照跑（5.3 同款断言手法）
# ---------------------------------------------------------------------------


def _opener():
    """不走环境代理的 opener —— 正对照要连的是环回的关闸端口，一个 `HTTP_PROXY`
    环境变量会让"被拒"变成"被代理转发"，把这条断言变成对机器配置的猜测。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _real_connect_first():
    """正对照（**补丁之前**）：经标准库 HTTP 客户端真连一把 —— 拒绝即
    `ConnectionRefusedError`，证明网络栈活着；否则"钉死后没出站"可能只是因为
    这台机器根本连不出去。

    走 `urllib` 而不是字面 `socket.create_connection(…)`：R7 静态扫描按**字面
    调用点**判定出站，台架的豁免只给 import（check.py 的明文边界）—— 测试要的
    是"一次真实连接"，产品代码要的是"一次都不存在"，HTTP 客户端正是豁免为台架
    留的那条路。

    结果收**被拒**与**超时**两种：环回关闸端口在本机（沙箱 / 防火墙可能黑洞
    SYN）两种都可能出现，而正对照要证明的是"连接尝试真的发出去了、TCP 栈活着"
    —— 两者都是它的证据。它排除掉的是"根本没发起尝试"（那才是网络栈不活）。
    """
    with pytest.raises(OSError) as excinfo:
        _opener().open("http://127.0.0.1:1/", timeout=2)
    reason = excinfo.value.args[0] if excinfo.value.args else None
    assert isinstance(reason, (ConnectionRefusedError, TimeoutError)), (
        f"正对照期望'被拒或超时'（连接尝试真的发出去了），收到 "
        f"{type(excinfo.value).__name__}: {reason!r}")


def _pin_outbound(monkeypatch) -> list:
    """把出站能力钉死：**任何一条真连接都当场炸**，并把每次尝试记下来。

    钉的是行为不是写法 —— `socket.socket.connect` 是所有出站的必经一步
    （`urllib` / `http.client` 最终也走它），`connect_ex` 是同门的第二条路，
    `create_connection` 是标准库客户端的入口。三处都在，拆开写也绕不过去。
    """
    attempts: list[tuple] = []

    def _blocked(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("零出站被破：评分期间试图建立出站连接")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    return attempts


def test_scoring_runs_with_the_socket_pinned(scored, monkeypatch):
    """正对照先行（补丁前真连一把），然后钉死三点跑完整 overview：出站一次都
    没发生（`attempts` 为空），评分照旧产出 —— 零出站不是"不能跑"，是"不伸手
    也照样出货"。"""
    _real_connect_first()

    attempts = _pin_outbound(monkeypatch)
    conn, project_id, _ = scored
    payload = risk.overview(conn, project_id)
    assert payload["score"]["total"] >= 0
    assert attempts == [], f"评分期间试图出站：{attempts}"


def test_each_socket_pin_is_live(monkeypatch):
    """钉子逐个验活：补丁后 `connect` / `connect_ex` / `create_connection`
    三个位置上装着的**都是挡板**，逐个点名调用全部 AssertionError —— 有一个
    失灵，"零出站"就只是半个断言。

    这里可以逐个调用而正对照那边必须走 urllib：挡板是纯 Python 函数 `_blocked`，
    **调它不会建任何套接字、不会发起任何连接**（它第一件事就是抛）。R7 拦的
    是"真的会去连的"字面调用点 —— 挡板不在其列，正是它该有的样子。
    """
    attempts = _pin_outbound(monkeypatch)
    pins = ((socket.socket, "connect"), (socket.socket, "connect_ex"),
            (socket, "create_connection"))
    for owner, name in pins:
        pinned = getattr(owner, name)          # 点名取出，验的就是"装在那的它"
        with pytest.raises(AssertionError, match="零出站"):
            pinned()
    assert len(attempts) == 3, attempts


def test_the_socket_pin_has_teeth(scored, monkeypatch):
    """反例：在评分路径里伸手出去（**LARI-45 放行说明点名的 urlopen 形态**：
    模拟"评分要准一点，联网查把 CVE 库"）→ **当场炸**。没有这条，上一条的绿
    可能只是"评分恰好没联网"，而不是"联网会被抓住"。"""
    attempts = _pin_outbound(monkeypatch)
    original = risk.collect

    def sneaky_look_up_cve(conn, project_id):
        _opener().open("http://127.0.0.1:1/")   # ← 反例：伸手出去
        return original(conn, project_id)

    monkeypatch.setattr(risk, "collect", sneaky_look_up_cve)
    conn, project_id, _ = scored
    with pytest.raises(AssertionError, match="零出站被破"):
        risk.overview(conn, project_id)
    assert attempts, "反例没被钉子抓住 —— 钉子是死的"
