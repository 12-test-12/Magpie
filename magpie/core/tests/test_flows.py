"""4.1 · 流量：`har` / `tshark -T json` → `flow` 会话摘要 + `storage='external'`。

这个文件里有**三组**判据，它们各自对应放行说明里的一句原话：

  ①  **会话摘要**：5 元组 + 时间窗 → 一段会话一行。核心那条是"**绝不逐包入库**"
      —— 它必须是一条**可检验**的性质，而不是一句注释。这里的检验方式是
      "喂 N 个包，记录数是**会话数**的量级，不是 N 的量级"（`test_never_stores_
      per_packet...`）：一个按包产出的实现当场红，而它在别处看起来一切正常。

  ②  **`stored` 与 `external` 两条归档路径**（阶段 4 的交付标准之一）：外部文件
      只记路径 + sha256 + 大小、正文一个字节都不入库、可恢复等级**如实**标注
      （不沿用 `stored` 的结论），而 `purge_raw_file` 对外部文件拒绝执行 ——
      因为"清除"这个词在它身上没有对应动作。

  ③  **缺 `tshark` 的可读错误路径**：这是"缺工具"这一半**今天能真跑**的形态
      （本机没有 tshark，放行说明里写死了）。它验的是报错指对了方向（缺什么、
      怎么装、以及本步其实不需要它），而不是一句 `FileNotFoundError`。

⚠️  **夹具的来历**：`fixtures/tshark/1/input.json` 是按 `tshark -T json` 的
已发布形状现写的**样本**，不是真导出 —— 本机没有 tshark。它证明的是"解析器与
形状自洽"，不是"与 Wireshark 的输出一致"。`test_tshark_fixture_declares_its_
provenance` 把这句话钉在夹具里：装上 Wireshark 之后，替换夹具的人会先看到它。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from magpie.core import ingest, migrate, normalize, ops, query, store
from magpie.core.parsers import base, har, tshark

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TSHARK_CASE = FIXTURES / "tshark" / "1"


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = store.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    store.init_db(conn)
    yield conn
    conn.close()


@pytest.fixture()
def project(db) -> int:
    return ops.create_project(db, "flow-test", {"include": [], "exclude": []})


def options(project_id: int, data_dir: Path, **kw) -> ingest.IngestOptions:
    return ingest.IngestOptions(project_id=project_id, data_dir=data_dir,
                                actor="test", verify=True, **kw)


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def packet(index: int, epoch: str, src: str, sport: str, dst: str, dport: str,
           protocols: str, length: str) -> dict:
    """一条 `tshark -T json` 的包（形状见夹具的文件头）。"""
    layers = {
        "frame_frame_protocols": protocols,
        "frame_frame_time_epoch": epoch,
        "frame_frame_len": length,
        "ip_ip_src": src, "ip_ip_dst": dst,
    }
    name = "tcp" if "tcp" in protocols else "udp"
    if name in protocols:
        layers[f"{name}_{name}_srcport"] = sport
        layers[f"{name}_{name}_dstport"] = dport
    return {"_index": "packets-x", "_type": "pcap_file", "_score": None,
            "_source": {"layers": layers}}


def capture(*packets: dict) -> str:
    return json.dumps(list(packets), indent=4)


def bad_lines(ctx: base.OfflineContext) -> list[str]:
    return [str(b["reason"]) for b in ctx.bad_lines]


def flow_keys(conn, project: int) -> list[str]:
    return sorted(str(r[0]) for r in conn.execute(
        "SELECT k.entity_key FROM entities e JOIN entity_keys k ON k.entity_id=e.id"
        " WHERE e.project_id=? AND e.kind='flow' AND e.is_current=1", (project,)))


def inbox(conn, project: int, kind: str | None = None) -> list[dict]:
    """**未决**的待办项（`state='open'`）—— 裁决过的不是"还开着的事"。

    只看 open 是刻意的：`undo` 关掉一条项之后它还留在表里（墓碑），把已裁决的
    一起列出来会让"undo 有没有真的关掉它"这条断言永远为真。
    """
    where = " AND kind=?" if kind else ""
    params = (project, kind) if kind else (project,)
    return [dict(r) for r in conn.execute(
        f"SELECT id, kind, subject_id, severity, state, payload_json FROM inbox_items"
        f" WHERE project_id=? AND state='open'{where} ORDER BY id", params)]


# ===========================================================================
# ① 会话摘要
# ===========================================================================

def test_golden_fixture_is_live_and_covers_both_sources():
    """两份黄金夹具都在产出 `flow`，而且声明的事实今天仍算得出来。

    逐字段比对在 `test_parsers.py` 的通用网里（`GOLDEN` 表）；这里补的是
    **这一半独有的那一问**：4.1 的两个来源都要真的走通会话摘要那条路 ——
    只做一个（比如只做 tshark）会让另一半的验收标准无人认领。
    """
    for tool, cls in (("har", har.HarParser), ("tshark", tshark.TsharkParser)):
        case = FIXTURES / tool / str(cls.format_version)
        expected = json.loads((case / "expected.json").read_text(encoding="utf-8"))
        kinds = {r["kind"] for r in expected["records"] if r["type"] == "entity"}
        assert "flow" in kinds, f"{tool}: 黄金夹具里一个 flow 实体都没有"
        assert any(r["rel"] == "involves" for r in expected["records"]
                   if r["type"] == "relation"), f"{tool}: 没有 involves 关系"


def test_tshark_fixture_declares_its_provenance():
    """那份 `tshark` 夹具**自报来历**：它是按形状现写的样本，不是真导出。

    为什么要一条断言：夹具是"当时被确认过的输出"，而这份没有"当时"——本机没有
    tshark，它从来没有被任何一个真导出确认过。这句话不能只写在交付评论里（评论
    会过期），也不能只写在解析器的文件头里（换夹具的人先看的是夹具目录）。

    装上 Wireshark 之后，这条断言会**逼着**替换夹具的人先把话说清楚：要么换上
    真导出并把 `provenance` 改掉，要么留着这句话。
    """
    doc = json.loads((TSHARK_CASE / "expected.json").read_text(encoding="utf-8"))
    provenance = str(doc.get("provenance") or "")
    assert "不是真导出" in provenance, provenance
    assert "tshark" in provenance, provenance


def test_session_is_split_by_the_idle_gap_not_by_the_file():
    """同一 5 元组的两段会话 → **两个** flow 实体（时间窗进身份的意义）。

    这正是"5 元组不是一个会话"那条裁定的可检验形式：只用 5 元组当身份的实现，
    在这里会算出**一个**实体，而它的 `packet_count` 是两段的和 —— 一个不描述
    任何一次真实会话的数字。
    """
    text = capture(
        packet(1, "1767225600.100000", "10.0.0.5", "51234", "93.184.216.34", "443",
               "eth:ethertype:ip:tcp:tls", "100"),
        # 相隔 300s > FLOW_IDLE_SECONDS(120) → 另起一段
        packet(2, "1767225900.100000", "10.0.0.5", "51234", "93.184.216.34", "443",
               "eth:ethertype:ip:tcp:tls", "200"),
    )
    records = tshark.parse_text(text)
    keys = [r.key for r in records if isinstance(r, base.RawEntity) and r.kind == "flow"]
    assert keys == [
        "tcp|10.0.0.5:51234|93.184.216.34:443|2026-01-01T00:00:00Z",
        "tcp|10.0.0.5:51234|93.184.216.34:443|2026-01-01T00:05:00Z",
    ], keys
    counts = {r.type: r.value for r in records if isinstance(r, base.RawObservation)}
    assert counts["packet_count"] == 1, counts


def test_reverse_direction_packets_join_the_same_session():
    """反向的包属于**同一段**会话：只按"谁发给谁"分组会把一次请求/响应拆成两段。

    判据是字节数：请求 100 + 响应 200 = 300。拆开的实现会给出两个各含一半的
    会话，而它们的 `packet_count` 都是 1 —— 那种实现看起来很"干净"，只有把
    计数与身份一起看才看得出错。
    """
    text = capture(
        packet(1, "1767225600.100000", "10.0.0.5", "51234", "93.184.216.34", "443",
               "eth:ethertype:ip:tcp", "100"),
        packet(2, "1767225600.200000", "93.184.216.34", "443", "10.0.0.5", "51234",
               "eth:ethertype:ip:tcp", "200"),
    )
    records = tshark.parse_text(text)
    flows = [r for r in records if isinstance(r, base.RawEntity) and r.kind == "flow"]
    assert len(flows) == 1, flows
    values = {r.type: r.value for r in records if isinstance(r, base.RawObservation)}
    assert values["packet_count"] == 2 and values["byte_count"] == 300, values
    # 身份里的方向是**第一个包**的那一侧（发起方）。
    assert flows[0].key.startswith("tcp|10.0.0.5:51234|93.184.216.34:443|"), flows[0].key


def test_never_stores_per_packet_it_stores_per_session():
    """**绝不逐包入库**（计划书 4.1 点名的一条）—— 这条断言就是它的形式化。

    500 个包、3 段会话：一个按包产出记录的实现会落下 500 条 flow 实体，而它
    在**别的所有用例**里都看不出来（那些用例的包数少，条数差异被当成噪声）。
    所以这里同时断言两端：会话数对得上，且记录总数与会话数是同一量级。

    阈值取"< 40"而不是"== 3"：每段会话的记录数由它自己的摘要字段决定（包数 /
    字节数 / 起止 / 协议 / 客户 / 主机 + 关系），把那个数字钉死会让"给摘要加一个
    字段"变成一次要改测试的改动 —— 而这条断言要守的是**量级**，不是那个数字。
    """
    packets = []
    for i in range(500):
        # 三段会话：两个不同的目的端口 + 一段隔了 10 分钟的续传。
        if i < 200:
            sport, dport, epoch = "51234", "443", 1767225600 + i
        elif i < 400:
            sport, dport, epoch = "51234", "8443", 1767225600 + i
        else:
            sport, dport, epoch = "51235", "443", 1767226200 + i
        packets.append(packet(i, f"{epoch}.100000", "10.0.0.5", sport,
                              "93.184.216.34", dport,
                              "eth:ethertype:ip:tcp:tls", "100"))
    ctx = base.OfflineContext()
    records = tshark.parse_text(capture(*packets), ctx)
    assert not bad_lines(ctx), ctx.bad_lines
    flows = [r for r in records if isinstance(r, base.RawEntity) and r.kind == "flow"]
    assert len(flows) == 3, [f.key for f in flows]
    assert len(records) < 40, f"{len(records)} 条记录（500 个包）—— 在按包入库？"
    assert len(records) < len(packets) / 10, len(records)


def test_arp_packets_are_skipped_once_with_a_readable_note():
    """非 IP 的包（ARP）**不是坏数据**，是这份模型覆盖不到的东西。

    判据两条：一条事实都不落（ARP 没有 5 元组），以及**只报一次**（按包报会把
    一份真实抓包的坏行报告刷成几千条，信号被噪声盖掉）。
    """
    arp = {"_index": "x", "_type": "pcap_file", "_score": None, "_source": {"layers": {
        "frame_frame_protocols": "eth:ethertype:arp",
        "frame_frame_time_epoch": "1767225600.050000",
        "frame_frame_len": "42",
        "arp_arp_opcode": "1"}}}
    ctx = base.OfflineContext()
    records = tshark.parse_text(capture(arp, arp, arp), ctx)
    assert records == []
    assert len(ctx.bad_lines) == 1, ctx.bad_lines
    assert "不构成 IP 会话" in str(ctx.bad_lines[0]["reason"])
    assert "Traceback" not in str(ctx.bad_lines[0]["reason"])


def test_field_lookup_accepts_both_documented_shapes():
    """取值对两种书写都认（`ip_ip_src` 与 `ip.src`）。

    ⚠️  **这条宽容是对冲，不是设计**：本机没有 tshark，形状无法实测，而猜错的
    方向是"整份文件读不出一个包"。测试把它钉住，免得下一个人在"清理死代码"
    时把另一半删掉 —— 删掉它需要先有一次真导出复核（见夹具的 `provenance`）。
    """
    dotted = {"_index": "x", "_type": "pcap_file", "_score": None, "_source": {"layers": {
        "frame.protocols": "eth:ethertype:ip:tcp",
        "frame.time_epoch": "1767225600.100000",
        "frame.len": "100",
        "ip.src": "10.0.0.5", "ip.dst": "93.184.216.34",
        "tcp.srcport": "51234", "tcp.dstport": "443"}}}
    records = tshark.parse_text(capture(dotted))
    flows = [r.key for r in records if isinstance(r, base.RawEntity) and r.kind == "flow"]
    assert flows == ["tcp|10.0.0.5:51234|93.184.216.34:443|2026-01-01T00:00:00Z"], flows


# ===========================================================================
# ② 两条归档路径：stored / external
# ===========================================================================

def test_external_archive_records_the_path_and_never_the_body(db, project, tmp_path):
    """`storage='external'`：只记**路径 + sha256 + 大小**，正文一个字节都不入库。

    判据是"那段正文在**整张库里**一次都不出现"（与 3.4 的凭据用例同一条口径）：
    比"某个字段没写"强得多 —— 后者只说明你检查的那个字段干净。
    """
    secret = "EXTERNAL-BODY-SHOULD-NEVER-LAND"
    source = write(tmp_path, "capture.json", capture(
        packet(1, "1767225600.100000", "10.0.0.5", "51234", "93.184.216.34", "443",
               "eth:ethertype:ip:tcp:tls", "100")) + f"\n{secret}\n")
    report = ingest.ingest_file(db, options(project, tmp_path, archive="external"),
                                source)

    assert report["custody"] == "external", report
    assert report["storage_state"] == "present", report
    row = dict(db.execute("SELECT * FROM raw_files WHERE id=?",
                          (report["raw_file_id"],)).fetchone())
    assert row["custody"] == "external"
    assert row["locator_uri"] == source.resolve().as_uri()
    assert row["sha256"] == report["sha256"]
    assert row["byte_count"] == source.stat().st_size
    # 源文件**原地不动**（external 的全部含义）
    assert source.is_file()
    assert not (tmp_path / "data" / "raw").exists(), "外部归档不该往 data_dir 里写"

    # 正文（尤其是那段凭空加进去的标记）一次都不在库里。
    dumped = "\n".join(str(r[0]) for r in db.execute(
        "SELECT payload_json FROM inbox_items")).lower()
    for table, column in (("raw_files", "locator_uri"), ("evidence", "locator")):
        for value in db.execute(f"SELECT {column} FROM {table}"):
            assert secret not in str(value[0])
    assert secret not in dumped


def test_stored_archive_copies_into_the_content_addressed_slot(db, project, tmp_path):
    """归档路径：复制进 `<data-dir>/raw/<sha 前两位>/<sha256>`，`custody='stored'`。

    两条归档路径**必须有各自的用例**（阶段 4 的交付标准），而它们的分歧点恰好
    是两件可检验的事：文件在谁手里（`custody`）、正文进没进 data_dir。
    """
    source = write(tmp_path, "capture.json", capture(
        packet(1, "1767225600.100000", "10.0.0.5", "51234", "93.184.216.34", "443",
               "eth:ethertype:ip:tcp:tls", "100")))
    data_dir = tmp_path / "data"
    report = ingest.ingest_file(
        db, options(project, data_dir, archive="archived"), source)
    digest = report["sha256"]
    dest = data_dir / "raw" / digest[:2] / digest

    assert report["custody"] == "stored", report
    assert report["storage_state"] == "archived", report
    assert dest.is_file(), dest
    assert dest.read_bytes() == source.read_bytes()
    row = dict(db.execute("SELECT custody, storage_state, locator_uri FROM raw_files"
                          " WHERE id=?", (report["raw_file_id"],)).fetchone())
    assert row["custody"] == "stored"
    assert row["locator_uri"] == dest.as_uri()


def test_external_capability_is_labelled_honestly(db, project, tmp_path):
    """外部文件的可恢复等级**不沿用** `stored` 的结论（4.1 点名的一条）。

    `stored` 下的"可恢复"= 本工具手里有副本，凭 locator + hash 取回片段。
    外部文件没有这件事：副本从来不在工具手里。沿用那个结论等于把一句"我保证"
    写在一条给不了保证的行上。
    """
    source = write(tmp_path, "har.json",
                   (FIXTURES / "har" / "1" / "input.json").read_text(encoding="utf-8"))
    report = ingest.ingest_file(db, options(project, tmp_path, archive="external"),
                                source)
    raw_id = int(report["raw_file_id"])

    # 两条路径对**同一份文件**给出的结论必须不一样。
    assert ops.evidence_capability("present", "full", "stored") == "recoverable"
    assert ops.evidence_capability("present", "full", "external") == "locator_only"
    assert ops.evidence_capability("present", "hash_only", "external") == "hash_only"

    degraded = ops.degrade_evidence(db, raw_file_id=raw_id, to_state="locator")
    assert degraded["custody"] == "external", degraded
    assert degraded["capability"] == "locator_only", degraded
    assert degraded["external_not_deletable"] is True, degraded
    assert "外部" in degraded["wording"], degraded["wording"]
    # 「原文可取得」在外部文件上说的是"我们还**知道**它在哪"，不是"我们有副本"。
    assert degraded["original_available"] is True and degraded["locatable"] is True

    dropped = ops.degrade_evidence(db, raw_file_id=raw_id, to_state="hash_only")
    assert dropped["capability"] == "hash_only", dropped
    assert dropped["original_available"] is False, dropped
    assert dropped["locatable"] is False, dropped


def test_purging_an_external_file_is_refused_with_a_readable_reason(db, project,
                                                                   tmp_path):
    """`purge_raw_file` 对外部文件**拒绝执行** —— "清除"在它身上没有对应动作。

    这条是 4.1 那半句"外部文件这个工具删不掉"的可检验形式。拒绝必须**可读**：
    说清为什么、以及真想放弃跟踪该用**哪一条**（`degrade_evidence`，它有真正的
    对应动作：丢掉 locator）。拒绝之后的库必须原样 —— 一行都没动。
    """
    source = write(tmp_path, "har.json",
                   (FIXTURES / "har" / "1" / "input.json").read_text(encoding="utf-8"))
    report = ingest.ingest_file(db, options(project, tmp_path, archive="external"),
                                source)
    before = dict(db.execute("SELECT storage_state, purged_at FROM raw_files"
                             " WHERE id=?", (report["raw_file_id"],)).fetchone())

    with pytest.raises(ops.InvariantError) as exc:
        ops.purge_raw_file(db, raw_file_id=int(report["raw_file_id"]))
    message = str(exc.value)
    assert "工具之外" in message and "degrade_evidence" in message, message
    # 拒绝之后库是干净的、而且**什么都没变**（拒绝不是"做了一半"）。
    assert dict(db.execute("SELECT storage_state, purged_at FROM raw_files"
                           " WHERE id=?", (report["raw_file_id"],)).fetchone()) == before
    assert store.integrity_check(db)["ok"] is True

    # 对照：**归档**的那条路径照旧能 purge（拒绝不是把整条链关掉）。
    # ⚠️  第二份文件的**内容必须不同**：同内容 = 同 sha256 = 整文件 `skipped`，
    # 那条路径上根本没有 raw_file 可 purge（`skipped` 的报告里没有 raw_file_id）。
    stored_path = write(tmp_path, "har2.json",
                        (FIXTURES / "har" / "1" / "input.json").read_text(
                            encoding="utf-8") + "\n")
    stored = ingest.ingest_file(db, options(project, tmp_path), stored_path)
    purged = ops.purge_raw_file(db, raw_file_id=int(stored["raw_file_id"]))
    assert purged["op"] == "raw_file.purge"
    assert purged["capability"] == "verifiable_only", purged
    assert purged["custody"] == "stored", purged


# ===========================================================================
# ③ 缺 tshark 的可读错误路径
# ===========================================================================

def test_missing_tshark_gives_a_readable_error_not_a_bare_filenotfound(monkeypatch):
    """缺 `tshark` 时抛**可读错误**：说清缺什么、怎么装、以及本步其实不需要它。

    放行说明里点名了这一条，理由是它**今天能真跑** —— 本机没有 tshark，所以这条
    路径是"缺工具"那一半里唯一能被真实执行的部分。三条判据缺一不可：

      * 不是 `FileNotFoundError`（那是"文件没找到"的形状，会把人引去查路径）；
      * 说清**缺的是什么**（tshark）与**怎么装**（winget / apt）；
      * 说清**本步不需要它**（主路径是离线解析已导出的 JSON）—— 少了这一句，
        一个上手的人会先去装 Wireshark，而那一步这一步并不要求。
    """
    monkeypatch.setattr(tshark.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError) as exc:
        tshark.ensure_available()
    message = str(exc.value)
    assert not isinstance(exc.value, FileNotFoundError)
    assert "tshark" in message and "winget" in message and "apt" in message, message
    assert "不需要它" in message, message
    assert "Traceback" not in message

    # 找到了就返回路径（这条函数只**查**，不执行任何东西）。
    monkeypatch.setattr(tshark.shutil, "which", lambda name: "C:/fake/tshark.exe")
    assert tshark.ensure_available() == "C:/fake/tshark.exe"


def test_tshark_is_not_a_hard_dependency_anywhere():
    """外部可执行**不是** extras：不执行任何外部程序，也不进 `pyproject.toml`。

    判据落在**AST**上（不是 `"subprocess" not in source`）：这个模块的 docstring
    里正大光明地写着"这里没有 subprocess"这句话，而字符串判据会把那句自述当成
    违规（一条会因为**注释提到它**就变红的规则，会教人删掉注释而不是删掉代码）。
    所以判的是"真的 import 了它 / 真的调了 `os.system`"。

    `pyproject.toml` 那一半守的是另一件事：`traffic` 这个 extras 存在但**是空的**
    （4.5 的收口点），tshark 不在任何 extras 里 —— 装不上 Wireshark 的人不该
    被 `pip install -e ".[traffic]"` 引到一个装不出 tshark 的地方去。
    """
    import ast
    source = Path(tshark.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {a.name.split(".")[0]
                for node in ast.walk(tree) if isinstance(node, ast.Import)
                for a in node.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree)
                 if isinstance(n, ast.ImportFrom) and n.module}
    assert "subprocess" not in imported, imported
    assert "os" not in imported, f"tshark.py 里 import 了 os：{imported}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in ("system", "popen", "spawn", "run"), (
                f"tshark.py 里调了执行外部程序的 {node.func.attr}()")

    # `pyproject.toml` 那一半判的是**解析出来的 extras 值**，不是文件里有没有
    # 这个字符串：`traffic = []` 上面那段注释正大光明地写着"tshark 不进这里"，
    # 而字符串判据会把那句说明当成违规（与上面 `subprocess` 那条同一种错法）。
    import tomllib
    pyproject = tomllib.loads((Path(tshark.__file__).resolve().parents[3]
                               / "pyproject.toml").read_text(encoding="utf-8"))
    extras = pyproject["project"].get("optional-dependencies") or {}
    assert "traffic" in extras, sorted(extras)
    for name, packages in extras.items():
        for package in packages:
            assert "tshark" not in package and "wireshark" not in package.lower(), \
                f"extras {name!r} 里出现了 {package!r} —— 外部可执行装不出来"


# ===========================================================================
# ③b 负向夹具：空 / 截断 / 超大 / 非 UTF-8
# ===========================================================================

NEGATIVES = [
    ("empty", "", "空文件"),
    ("truncated", '[{"_index": "x", "_source": {"layers": {"ip_ip_src": "10.0.0.5"',
     "截断（最后一个包没闭合）"),
    ("not-an-array", '{"layers": {}}', "顶层不是数组"),
    ("array-of-scalars", "[1, 2, 3]", "数组里的元素不是对象"),
    ("huge-packet", "[" + "{" + "a" * (tshark.MAX_PACKET_CHARS + 10), "超大（单包超限）"),
    ("garbage", "this is not json at all\n", "根本不是 JSON"),
]


@pytest.mark.parametrize("name,payload,label", NEGATIVES,
                         ids=[c[0] for c in NEGATIVES])
def test_tshark_negative_inputs_never_crash(db, project, tmp_path, name, payload,
                                            label):
    """四条负向（空 / 截断 / 超大 / 非 UTF-8）逐条不崩，且给出可读提示。

    判据是 2.4 那张表里写死的那两条的**扩展**：零记录、有坏行、理由里没有堆栈。
    "空文件"是唯一的例外 —— 它**不是坏行**（与 har / ffuf 同一条口径：空文件
    报一条坏行会让"这份文件是空的"变成"这份文件坏了"）。
    """
    ctx = base.OfflineContext()
    records = tshark.parse_text(payload, ctx)
    assert records == [], (name, records)

    if name == "empty":
        assert ctx.bad_lines == [], ctx.bad_lines
    else:
        assert ctx.bad_lines, f"{name}: 没有坏行 —— 使用者只会看到「文件没东西」"
    for line in ctx.bad_lines:
        reason = str(line["reason"])
        assert reason.strip() and "Traceback" not in reason, (name, reason)

    # 走完整管线也不崩（指定解析器，验的是**管线**那一层）。
    report = ingest.ingest_file(
        db, options(project, tmp_path, parser="tshark"),
        write(tmp_path, f"{name}.json", payload))
    assert report["state"] in ("done", "partial", "skipped", "failed",
                               "needs_confirmation"), report["state"]
    assert report["counts"]["records"] == 0, report["counts"]
    assert "Traceback" not in json.dumps(report, ensure_ascii=False, default=str)


def test_tshark_non_utf8_is_decoded_not_mangled(db, project, tmp_path):
    """非 UTF-8（GBK）：按探测到的编码解码后**照常解析**，不是被拒也不是被改名。

    与 2.4 / 3.4 的同一口径 —— 编码是管线的事（`ingest.probe_file` 那一层），
    解析器只管拿到的那段文本。
    """
    text = capture(packet(1, "1767225600.100000", "10.0.0.5", "51234",
                          "93.184.216.34", "443", "eth:ethertype:ip:tcp:tls", "100"))
    path = tmp_path / "gbk.json"
    path.write_bytes(text.encode("gbk"))
    report = ingest.ingest_file(db, options(project, tmp_path, parser="tshark"), path)
    assert report["counts"]["records"] > 0, report
    assert flow_keys(db, project) == [
        "tcp|10.0.0.5:51234|93.184.216.34:443|2026-01-01T00:00:00Z"], \
        flow_keys(db, project)


# ===========================================================================
# ④ 会话里出现的新 host → 自动进 Inbox
# ===========================================================================

def test_a_new_host_seen_in_a_session_lands_in_the_inbox(db, project, tmp_path):
    """**4.1 的交付标准**：会话里出现的新 host → 自动进 Inbox，走既有的通道。

    三件事一起断言，缺一条都只是"开了条待办"：

      * `kind` 是新那一个（`unregistered_asset`），主体是**主机实体**（要人裁决的
        是"这台主机纳不纳入"），而不是 flow 实体；
      * 它是**惰性**的：另一份不含会话的导入（httpx）一行都不写；
      * `undo` 把这条待办关掉（生命周期锚在作业上，与既有的 `_open_inbox` 一族
        同一条边）。
    """
    # 先来一份**不含会话**的导入：它是惰性的对照组。
    ingest.ingest_file(db, options(project, tmp_path),
                       write(tmp_path, "n.jsonl", json.dumps({
                           "timestamp": "2026-01-01T00:00:00Z", "port": "443",
                           "url": "https://plain.example.com", "input": "plain.example.com",
                           "title": "T", "scheme": "https", "status_code": 200,
                           "webserver": "nginx/1.24.0", "tech": ["nginx"]}) + "\n"))
    assert inbox(db, project, "unregistered_asset") == [], "没有会话却开了待办"

    source = write(tmp_path, "capture.json", capture(
        packet(1, "1767225600.100000", "10.0.0.5", "51234", "93.184.216.34", "443",
               "eth:ethertype:ip:tcp:tls", "100")))
    report = ingest.ingest_file(db, options(project, tmp_path), source)

    items = inbox(db, project, "unregistered_asset")
    assert len(items) == 1, items
    item = items[0]
    assert item["severity"] == "low", item
    payload = json.loads(item["payload_json"])
    assert payload["entity_key"] == "93.184.216.34", payload
    assert payload["host_kind"] == "ip", payload
    assert item["subject_id"] == int(db.execute(
        "SELECT e.id FROM entities e JOIN entity_keys k ON k.entity_id=e.id"
        " WHERE k.entity_key='93.184.216.34'").fetchone()[0])
    assert query.health_report(db, project)["unclaimed"] == []

    # 重导同一份文件：幂等（第二次它已经不是"从未登记过"了）。
    ingest.ingest_file(db, options(project, tmp_path,
                                   parser=report["parser"]), source)
    assert len(inbox(db, project, "unregistered_asset")) == 1

    # undo 关掉它（生命周期锚在这个作业上）。
    ops.undo(db, int(report["job_id"]), actor="test")
    assert inbox(db, project, "unregistered_asset") == []


def test_a_host_seen_before_is_not_reported_again(db, project, tmp_path):
    """**此前见过的主机不再报**：判据是"本作业之外没有任何贡献指向它"。

    反例的形状很具体：先导入一份 httpx（它登记了 `10.0.0.6`），再导入一份含到
    `10.0.0.6` 会话的抓包。第二份**不该**再开待办 —— 那台主机在项目里是有主的，
    而"发现未登记资产"说的不是"我第一次在这份文件里看到它"。
    """
    ingest.ingest_file(db, options(project, tmp_path), write(
        tmp_path, "h.jsonl", json.dumps({
            "timestamp": "2026-01-01T00:00:00Z", "port": "443",
            "url": "https://known.example.com", "input": "known.example.com",
            "host": "known.example.com", "title": "T", "scheme": "https",
            "status_code": 200, "webserver": "nginx/1.24.0",
            "a": ["10.0.0.6"], "tech": ["nginx"]}) + "\n"))
    assert db.execute("SELECT COUNT(*) FROM entity_keys WHERE entity_key='10.0.0.6'"
                      ).fetchone()[0] == 1, "夹具没有先登记那台主机"

    source = write(tmp_path, "capture.json", capture(
        packet(1, "1767225600.100000", "10.0.0.5", "51234", "10.0.0.6", "443",
               "eth:ethertype:ip:tcp:tls", "100"),
        packet(2, "1767225601.100000", "10.0.0.5", "51234", "10.0.0.9", "443",
               "eth:ethertype:ip:tcp:tls", "100")))
    ingest.ingest_file(db, options(project, tmp_path), source)

    keys = {json.loads(str(i["payload_json"]))["entity_key"]
            for i in inbox(db, project, "unregistered_asset")}
    assert keys == {"10.0.0.9"}, keys


def test_the_judgement_lives_in_query_so_both_sides_agree(db, project, tmp_path):
    """判据只有一份：`query.unregistered_assets` —— 读写两侧共用它。

    "体检页说 3、Inbox 说 2"这种分歧的成因永远是两份判据。这里断言的是**同一次
    调用**既能解释已经开出来的待办，也说明得了为什么没开别的（与
    `refresh_scope_drift` / `out_of_scope_entities` 同一条分工）。
    """
    source = write(tmp_path, "capture.json", capture(
        packet(1, "1767225600.100000", "10.0.0.5", "51234", "93.184.216.34", "443",
               "eth:ethertype:ip:tcp:tls", "100")))
    report = ingest.ingest_file(db, options(project, tmp_path), source)
    found = query.unregistered_assets(db, project_id=project,
                                      job_id=int(report["job_id"]))
    assert [row["entity_key"] for row in found] == ["93.184.216.34"], found
    assert [row["host_kind"] for row in found] == ["ip"], found
    # 判据 4：人工接管过的主机不再报（`manual_owned` 是唯一判据处）。
    entity_id = int(found[0]["id"])
    db.execute("INSERT INTO entity_tags(project_id, entity_id, tag, set_by, set_at)"
               " VALUES (?, ?, 'seen', 'human', '2026-01-01T00:00:00Z')",
               (project, entity_id))
    assert query.unregistered_assets(db, project_id=project,
                                     job_id=int(report["job_id"])) == []


def _database_at_version_10(tmp_path) -> sqlite3.Connection:
    """建一个**只应用到 010** 的库（升级路径要被测到，不能只测新库）。"""
    conn = store.connect(tmp_path / "old.db")
    conn.execute("PRAGMA foreign_keys = ON")
    original = migrate.MIGRATIONS
    try:
        migrate.MIGRATIONS = tuple(m for m in original if m.version <= 10)
        store.init_db(conn)
    finally:
        migrate.MIGRATIONS = original
    assert migrate.current_version(conn) == 10
    return conn


def test_migration_011_rebuilds_the_inbox_table_without_losing_anything(tmp_path):
    """011 重建了 `inbox_items`（SQLite 改不了 CHECK）—— 行必须**一条不少、一字不改**。

    这是 011 里风险最高的那一步，所以它值得一条**独立**的用例，而不是靠
    "新库上跑得过"来推断：重建走的是"改名 → 建新 → 拷行 → 删旧 → 建索引"，
    任何一步写错都会留下**看起来一样、实际不一样**的表 —— 而那正是 R4 那条纪律
    （不许改 001）在防的东西。

    四件事一起断言：

      * 升级**不动一行**：整张表逐列快照与升级前相等（含墓碑行、时间戳、外键）；
      * 新取值进去得去（第八个 kind）；
      * 老的拒绝还在（不在清单里的 kind 仍然被 CHECK 拒）；
      * `ux_inbox_open_fingerprint` 这个**部分唯一索引**还在起作用（它是"同一
        fingerprint 至多一个 open 项"的唯一实现，重建时最容易漏掉它）；
      * `v_integrity_violations` 仍然指得到这张表（改名会把视图正文改写 ——
        迁移里那句 `legacy_alter_table = ON` 就是为它写的，实测见该文件头）。
    """
    conn = _database_at_version_10(tmp_path)
    try:
        project = ops.create_project(conn, "upgrade-011", {"include": [], "exclude": []})
        job = ops.start_job(conn, project_id=project, kind="ingest", coverage="partial")
        raw = ops.add_raw_file(conn, project_id=project, locator_uri="file:///seed.txt",
                               content="x")
        evidence = ops.add_evidence(conn, project_id=project, raw_file_id=raw,
                                    locator={"line": 1}, snippet="x")
        source = store.register_source(conn, name="seed", actor="test")
        entity = ops.upsert_entity(conn, project_id=project, job_id=job,
                                   evidence_id=evidence, source_id=source,
                                   entity_key="a.example.com", kind="domain")
        # 缓存投影要跟上（`ops.upsert_entity` 是低阶原语，不像管线那样自己重算）
        # —— 少了这一步，下面那条 `integrity_check` 会被 `cache_understated_entity`
        # 判红，而那条红说的是**种子的写法**，不是迁移的问题。
        store.recompute(conn, delta=[("entity", entity)])
        # 三条形态不同的待办：open / 已裁决 / 被 undo 开着的（都带指纹与时间戳）。
        opened, _ = ops._open_inbox(conn, project_id=project, kind="value_conflict",
                                    subject_kind="entity", subject_id=entity,
                                    values=["a", "b"], sources=[], job_id=job,
                                    conflict_field="webserver")
        resolved, _ = ops._open_inbox(conn, project_id=project, kind="manual_required",
                                      subject_kind="entity", subject_id=0,
                                      values=[], sources=[], job_id=None)
        conn.execute("UPDATE inbox_items SET state='resolved', resolved_at=?,"
                     " resolved_by='human', close_reason='done' WHERE id=?",
                     ("2026-01-01T00:00:00Z", resolved))
        conn.execute("UPDATE inbox_items SET occurrence_count=7,"
                     " last_seen_at='2026-01-02T00:00:00Z',"
                     " payload_json='{\"k\": 1}' WHERE id=?", (opened,))

        columns = [str(r["name"]) for r in
                   conn.execute("PRAGMA table_info(inbox_items)")]
        before = [tuple(row) for row in conn.execute(
            f"SELECT {', '.join(columns)} FROM inbox_items ORDER BY id")]

        assert [a["version"] for a in migrate.migrate(conn)] == [11]
        assert [r["name"] for r in conn.execute(
            "PRAGMA table_info(inbox_items)")] == columns
        after = [tuple(row) for row in conn.execute(
            f"SELECT {', '.join(columns)} FROM inbox_items ORDER BY id")]
        assert after == before, "重建动了行："
        # 行数对不上时，报错里给的是完整快照（便于逐列比对）。
        assert conn.execute("SELECT COUNT(*) FROM inbox_items").fetchone()[0] == len(before)

        # 第八个取值可用，老的拒绝仍在。
        ops._open_inbox(conn, project_id=project, kind="unregistered_asset",
                        subject_kind="entity", subject_id=entity, values=[],
                        sources=[], job_id=job)
        assert store.integrity_check(conn)["ok"] is True
        with pytest.raises(sqlite3.IntegrityError):
            ops._open_inbox(conn, project_id=project, kind="bogus_kind",
                            subject_kind="entity", subject_id=entity, values=[],
                            sources=[], job_id=job)
        # 部分唯一索引仍然在（同一 fingerprint 的 open 项插不进第二条）。
        conn.execute("INSERT INTO inbox_items(project_id, kind, subject_kind,"
                     " subject_id, fingerprint, state, created_at)"
                     " VALUES (?, 'orphan_subject', 'entity', ?, 'fp-x', 'open',"
                     " '2026-01-01T00:00:00Z')", (project, entity))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO inbox_items(project_id, kind, subject_kind,"
                         " subject_id, fingerprint, state, created_at)"
                         " VALUES (?, 'orphan_subject', 'entity', ?, 'fp-x', 'open',"
                         " '2026-01-01T00:00:00Z')", (project, entity))
        # 视图还指得到这张表（没有被改名改坏）。
        assert conn.execute("SELECT COUNT(*) FROM v_integrity_violations"
                            ).fetchone()[0] == 0
        # 010 建的那张声明表与视图也还在（011 只碰该碰的）。
        assert conn.execute("SELECT COUNT(*) FROM multivalue_fields").fetchone()[0] > 0
        assert [r["kind"] for r in query.kinds(conn)].count("flow") == 1
    finally:
        conn.close()


def test_flow_identity_is_registered_with_its_three_parts(db):
    """`flow` 这个 kind 的三件套齐全，且镜头归属是**第一个**用 `traffic` 的。

    与 `test_parsers.py` 的通用网互补的那一半：镜头 `traffic` 从 001 的 CHECK 起
    就躺着，`flow` 是第一个用它的人 —— 这条断言把这个事实写下来，免得以后有人
    以为它来自某个新加的枚举。
    """
    row = dict(db.execute("SELECT * FROM kind_registry WHERE kind='flow'").fetchone())
    assert row["lens"] == "traffic" and row["lens"] in base.LENSES, row["lens"]
    assert row["identity_rule"].strip()
    assert row["fixture_path"] == "fixtures/kinds/flow.json"
    assert json.loads(row["produced_by"]) == ["har@1", "tshark@1"]
    doc = json.loads((FIXTURES / "kinds" / "flow.json").read_text(encoding="utf-8"))
    assert doc["identity_rule"] == row["identity_rule"]
    # 夹具里那两条**必被拒**的样例之一：两端都不知道地址。
    assert normalize.flow_key("tcp", "10.0.0.5", 1, "10.0.0.6", 2,
                              "2026-01-01T00:00:12Z") == \
        "tcp|10.0.0.5:1|10.0.0.6:2|2026-01-01T00:00:12Z"
    with pytest.raises(normalize.NormalizeError):
        normalize.flow_key("tcp", "", None, "", None, "2026-01-01T00:00:12Z")
