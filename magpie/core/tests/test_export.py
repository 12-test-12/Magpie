"""4.4 · 时间线 / 导出 / 打包：判据逐条钉在这里。

判据（LARI-39）与本文件的落点：

  ① **默认包找不到 headers / cookie / 凭据明文**（照 4.2 的形状：扫包内**所有**
     文件，不是断言某一处没写）。默认包结构上不含原文正文，观测值再过值级掩码
     —— 两道防线，测试两道都扫。
  ② **`--full` 能找到原文 + 命令行有可见告警**。原文 = stored 归档正文；告警走
     stderr（命令行上一眼可见，且不污染 `--json` 信封）。
  ③ **导出 → 读回往返一致**，或明确说明哪一部分刻意不往返（external 正文、
     默认 stored 正文、凭据明文掩码）。
  ④ **时间线沿用 §8.1**：exact/approximate 按 observed_at 排进 events；
     unknown 只进 lower_bounds，**不与前两者交错排序**。

外加打包自洽的两条（放行说明 3 + 红线 R7）：
  * external 原文只带路径 + sha256，`--full` 也不复制正文；
  * README / manifest **零外部链接、零 file://**（查看者不被动发起请求）。

⚠️  夹具的来历：HAR 按规范**现写**（本机连不上网），里面的凭据是假的、专门
为了"扫包能不能抓到它"而放的。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from magpie.core import export, ingest, migrate, ops, query, store

# 扫包用的敏感串。**这些必须是"只该出现在原文里"的字面量** —— README/manifest
# 解释概念时会写 "headers / cookie" 这些词，但不会出现下面这些**带冒号的头行**
# 或**具体凭据值**，所以扫它们不会误报说明文件。
CRED_PLAINTEXT = "TEST-CRED-PLAINTEXT-XYZ"     # Authorization: Bearer <它>
QUERY_TOKEN = "visible-token-abc123"           # ?token=<它>（观测值里，会被掩码）
# 扫的标记要"只该出现在原文里、绝不出现在说明文件里"。README/manifest 解释概念时
# 会写小写 `cookie` 与 `headers`，但**不会**写 `Authorization` / 大写 `Cookie` /
# `Bearer`，也不含上面两个具体凭据值 —— 所以按这些标记扫不会误报说明文件，
# 同时能同时逮住 HAR JSON（`"Authorization"`）与 HTTP 头行（`Authorization:`）
# 两种形态。
_SCAN_MARKERS = (
    CRED_PLAINTEXT,
    QUERY_TOKEN,
    "Authorization",
    "Cookie",
    "Bearer",
)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = store.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    store.init_db(conn)
    yield conn
    conn.close()


@pytest.fixture()
def project(db) -> int:
    return ops.create_project(db, "export-test", {"include": [], "exclude": []})


def options(project_id: int, data_dir: Path, **kw) -> ingest.IngestOptions:
    return ingest.IngestOptions(project_id=project_id, data_dir=data_dir,
                                actor="test", verify=True, **kw)


def _har(credential: str = CRED_PLAINTEXT, token: str = QUERY_TOKEN) -> str:
    """一份最小 HAR：Authorization 头带凭据、查询串带 token、外加一个 Cookie。

    这三样正好覆盖"默认包里不该有的三种东西"：头、cookie、凭据明文。
    """
    doc = {
        "log": {"version": "1.2", "creator": {"name": "t", "version": "1"},
                "entries": [{
                    "startedDateTime": "2026-01-01T00:00:00.000Z",
                    "time": 10,
                    "request": {
                        "method": "GET",
                        "url": f"https://api.example.com/v1/me?token={token}",
                        "httpVersion": "HTTP/1.1",
                        "headers": [
                            {"name": "Authorization", "value": f"Bearer {credential}"},
                            {"name": "Cookie", "value": "session=abc123"},
                        ],
                        "queryString": [{"name": "token", "value": token}],
                        "headersSize": 0, "bodySize": 0,
                    },
                    "response": {
                        "status": 200, "statusText": "OK", "httpVersion": "HTTP/1.1",
                        "headers": [{"name": "Content-Type",
                                     "value": "application/json"}],
                        "content": {"size": 2, "mimeType": "application/json"},
                        "redirectURL": "",
                        "headersSize": 0, "bodySize": 2,
                    },
                    "cache": {},
                    "timings": {"send": 1, "wait": 2, "receive": 3},
                }]}
    }
    return json.dumps(doc)


def _ingest(db, project, tmp_path, *, archive="archived", text=None):
    src = tmp_path / "cap.har"
    src.write_text(text if text is not None else _har(), encoding="utf-8")
    report = ingest.ingest_file(db, options(project, tmp_path, archive=archive), src)
    assert report["state"] == "done", report
    return report


def _scan_files(files: dict, markers=_SCAN_MARKERS) -> dict:
    """扫**包内所有文件**的字节，返回 `{marker: [命中它的文件…]}`（空 = 干净）。"""
    hits: dict[str, list[str]] = {m: [] for m in markers}
    for name, content in files.items():
        blob = content if isinstance(content, (bytes, bytearray)) \
            else str(content).encode("utf-8")
        text = blob.decode("utf-8", "replace")
        for marker in markers:
            if marker in text:
                hits[marker].append(name)
    return {m: fs for m, fs in hits.items() if fs}


# ===========================================================================
# 判据① 默认包找不到 headers / cookie / 凭据明文
# ===========================================================================


def test_default_package_contains_no_header_cookie_or_credential(db, project,
                                                                 tmp_path):
    """扫默认包的**每一个文件**：头行 / cookie / 凭据明文一个都找不到。

    照 4.2 的形状：搜的是"整个包"，不是"某一个字段" —— 凭据漏出的姿势通常在
    没人想到的地方（说明文件、CSV、manifest）。两道防线在这里一起被验：
    结构上不带原文正文 + 观测值过值级掩码。
    """
    _ingest(db, project, tmp_path)
    files = export.build_package(db, project, full=False,
                                 tool_version="test", project_name="export-test")

    # 包必须含自描述的两件套（拿到包的人不装工具也能看懂）
    assert export.README_NAME in files and export.MANIFEST_NAME in files

    hits = _scan_files(files)
    assert not hits, f"默认包里找到了不该出门的东西：{hits}"

    # 结构防线：默认包根本不带原文正文
    assert not any(name.startswith("raw/") for name in files), \
        "默认包不应含 raw/ 正文"


def test_default_package_redacts_credential_shaped_field_value(db, project,
                                                              tmp_path):
    """值级掩码（第二道防线）：观测值里的 `token=…` 被换成掩码，明文不出门。

    HAR 的 URL 里带 `?token=…`，它会落成 endpoint 的 `path` 观测值 —— 这是一条
    真实的、凭据形状的值溜进结构化数据的路径。默认导出必须把它掩掉。
    """
    _ingest(db, project, tmp_path)
    files = export.build_package(db, project, full=False,
                                 tool_version="test", project_name="p")
    entities_text = files["entities.json"].decode("utf-8")

    assert QUERY_TOKEN not in entities_text, "token 明文出现在 entities.json"
    assert "****" in entities_text, "值级掩码没有生效（没有任何掩码标记）"


def test_secret_entities_export_hash_and_mask_only_in_both_modes(db, project,
                                                                 tmp_path):
    """4.2 的产物：`secret` 实体两种模式都只有哈希 + 掩码前缀，永无明文。

    判据说"secret 的哈希与掩码前缀可以出，明文永不出"，且 `--full` 也不破例
    （--full 出的是归档原文，不是被扫出来的凭据值）。
    """
    _ingest(db, project, tmp_path)
    for full in (False, True):
        files = export.build_package(db, project, full=full,
                                     tool_version="test", project_name="p")
        secrets = json.loads(files["secrets.json"].decode("utf-8"))
        assert secrets, f"full={full}: secrets.json 为空"
        text = json.dumps(secrets, ensure_ascii=False)
        assert CRED_PLAINTEXT not in text, f"full={full}: 凭据明文进了 secrets.json"
        # 哈希（实体键里的 16 位十六进制）与掩码前缀都在
        assert any("****" in json.dumps(s, ensure_ascii=False) for s in secrets), \
            f"full={full}: 掩码前缀没出"


# ===========================================================================
# 判据② --full 能找到原文 + 命令行有可见告警
# ===========================================================================


def test_full_package_includes_stored_raw_body_and_stays_structured_clean(
        db, project, tmp_path):
    """`--full` 带 stored 原文正文（凭据在 raw/ 里**找得到**），结构化文件仍干净。"""
    _ingest(db, project, tmp_path)
    files = export.build_package(db, project, full=True,
                                 tool_version="test", project_name="p")

    raw_members = [n for n in files if n.startswith("raw/")]
    assert raw_members, "--full 包里没有 raw/ 正文"
    body = b"".join(files[n] for n in raw_members)
    assert CRED_PLAINTEXT.encode() in body, "--full 的原文正文里找不到凭据（原文不原文？）"
    assert b"Authorization" in body, "--full 的原文正文里找不到头名"

    # 结构化文件（实体/密钥/说明）在 --full 下**仍然**只出哈希+掩码、仍过掩码
    for name in ("entities.json", "entities.csv", "secrets.json",
                 "timeline.json", "raw_files.json"):
        assert CRED_PLAINTEXT not in files[name].decode("utf-8"), \
            f"--full 下 {name} 不该出现凭据明文（它只在 raw/ 原文里）"
        assert QUERY_TOKEN not in files[name].decode("utf-8"), \
            f"--full 下 {name} 不该出现未掩码的 token"


def test_full_export_prints_visible_warning_on_stderr():
    """命令行上有**一眼可见**的告警，不是日志里一行（判据②的后半）。

    走真子进程：告警必须落在 **stderr**（终端上就在眼前），而 `--json` 信封在
    stdout —— 混进去会把 `json.load` 弄炸。默认导出则**不**告警。
    """
    import subprocess
    import sys
    import tempfile

    with tempfile.TemporaryDirectory(prefix="magpie-expwarn-") as tmp:
        root = Path(tmp)
        env_db = str(root / "w.db")
        def run(*args):
            return subprocess.run(
                [sys.executable, "-m", "magpie.cli", "--db", env_db, *args],
                capture_output=True, env=None)
        # 建库 + 项目 + 导入
        run("init")
        run("project", "create", "p")
        har = root / "c.har"
        har.write_text(_har(), encoding="utf-8")
        run("import", str(har))
        # --full：stderr 必须有告警
        out_dir = root / "fullpkg"
        proc = run("export", "--full", "-o", str(out_dir), "--json")
        stderr = proc.stderr.decode("utf-8", "replace")
        assert "凭据明文" in stderr or "--full" in stderr, \
            f"--full 的 stderr 没有可见告警：{stderr[:300]!r}"
        # 默认：stderr 不该有那条告警
        proc2 = run("export", "-o", str(root / "defpkg"), "--json")
        stderr2 = proc2.stderr.decode("utf-8", "replace")
        assert "包里含归档原文" not in stderr2, \
            f"默认导出不该告警：{stderr2[:300]!r}"


def test_reexport_default_into_previous_full_dir_leaves_no_stale_raw(
        db, project, tmp_path):
    """跨模式重导不留幽灵文件（判据①的**唯一失败模式**：默认泄露）。

    复现审查报告的路径：`--full` 往目录写了 `raw/<sha256>`（含凭据明文）之后，
    默认模式重导**同一目录**。修之前：磁盘上留着上一轮的正文，而新 manifest 与
    信封都声称"包是干净的" —— 用户照 manifest 打包发出，带出去的却是上一轮的
    凭据明文，而 14 条旧用例都在空目录里跑、对此零覆盖。`-o` 默认值还是固定名
    `magpie-export`，撞车是最自然的操作序列。

    修之后断言两件事：① 整个目录扫一遍，凭据向量 **0 命中**；②
    **manifest 所列 == 磁盘实有**（无幽灵文件、无漏列、默认包磁盘上无 `raw/`）。
    """
    _ingest(db, project, tmp_path)
    out = tmp_path / "pkg"

    # 第一步：--full → raw/ 带明文正文
    full_files = export.build_package(db, project, full=True,
                                      tool_version="t", project_name="p")
    export.write_package(full_files, out)
    assert any(n.startswith("raw/") for n in full_files), "前提：--full 得带正文"
    assert CRED_PLAINTEXT.encode() in b"".join(
        full_files[n] for n in full_files if n.startswith("raw/"))

    # 第二步：默认模式重导**同一目录**
    default_files = export.build_package(db, project, full=False,
                                         tool_version="t", project_name="p")
    export.write_package(default_files, out)

    # 断言①：整个目录（含所有落盘文件）扫一遍，凭据向量 0 命中
    hits = []
    for p in out.rglob("*"):
        if p.is_file():
            blob = p.read_bytes()
            for marker in _SCAN_MARKERS:
                if marker.encode("utf-8") in blob:
                    hits.append((p.relative_to(out).as_posix(), marker))
    assert not hits, f"跨模式重导后目录残留明文：{hits}"

    # 断言②：manifest 所列 == 磁盘实有（幽灵文件会让这两者分叉）
    manifest = json.loads(
        (out / export.MANIFEST_NAME).read_text(encoding="utf-8"))
    listed = {e["name"] for e in manifest["files"]} | {export.MANIFEST_NAME}
    on_disk = {p.relative_to(out).as_posix()
               for p in out.rglob("*") if p.is_file()}
    assert listed == on_disk, (
        f"manifest 与磁盘不一致：只在 manifest={listed - on_disk}, "
        f"只在磁盘={on_disk - listed}")
    assert not any(n.startswith("raw/") for n in on_disk), \
        "默认包磁盘上不该有任何 raw/ 正文"


# ===========================================================================
# 判据③ 导出 → 读回往返一致 + 明确说明刻意不往返的部分
# ===========================================================================


def test_package_round_trips_entities_exactly(db, project, tmp_path):
    """`build → write → read` 往返：读回的实体 == 写进去的实体（逐项相等）。

    干净数据上值级掩码是恒等的（`detect_secrets` 无命中），所以读回的实体 ==
    `export_entities`。带凭据形状的数据上，掩码**刻意不往返**明文（见下一条与
    README 的「刻意不往返」段）。
    """
    # 一份**没有**凭据形状值的输入（纯域名清单）→ 掩码恒等 → 严格往返
    src = tmp_path / "clean.txt"
    src.write_text("app.example.com\napi.example.net\n", encoding="utf-8")
    report = ingest.ingest_file(db, options(project, tmp_path), src)
    assert report["state"] == "done", report

    files = export.build_package(db, project, full=False,
                                 tool_version="test", project_name="p")
    out = tmp_path / "pkg"
    export.write_package(files, out)
    back = export.read_package(out)

    # ① 包自己读得回（写的 == 读的）
    assert back["entities"] == json.loads(files["entities.json"].decode("utf-8"))
    # ② 与库里的导出逐项相等（干净数据 → 掩码恒等 → 严格往返）
    raw = query.export_entities(db, project)
    assert back["entities"] == raw, "干净数据上导出→读回 ≠ export_entities"
    assert back["timeline"] is not None
    assert back["manifest"]["redaction"] == "default"


def test_manifest_documents_what_is_intentionally_not_round_tripped(
        db, project, tmp_path):
    """判据③允许"明确说明刻意不往返的部分" —— README + manifest 必须说出三条。"""
    _ingest(db, project, tmp_path, archive="external")
    files = export.build_package(db, project, full=False,
                                 tool_version="test", project_name="p")
    readme = files[export.README_NAME].decode("utf-8")
    manifest = json.loads(files[export.MANIFEST_NAME].decode("utf-8"))
    notes = " ".join(manifest["notes"])

    # external 正文从不进包 → 不往返
    assert "external" in readme and "路径" in readme, "README 没说 external 只带路径"
    # 默认 stored 正文不在包里 → 不往返
    assert "默认" in readme and ("不带" in readme or "不含" in readme or
                                 "不在包里" in readme), "README 没说默认不带正文"
    # 凭据明文掩码 → 刻意不往返
    assert "掩码" in readme or "明文" in readme, "README 没说凭据明文不往返"
    assert any("external" in n for n in manifest["notes"]), \
        "manifest.notes 没记 external 不往返"


# ===========================================================================
# 判据④ 时间线沿用 §8.1
# ===========================================================================


def _plant(db, project, *, key, value, at, accuracy, source, precision):
    """直接写一条带 §8.1 时间四元组的观测（再 recompute 刷缓存）。"""
    src = store.register_source(db, name=f"s-{key}", authority_level="verified",
                                tool="t")
    raw = ops.add_raw_file(db, project_id=project, locator_uri="file:///x",
                           content="x")
    ev = ops.add_evidence(db, project_id=project, raw_file_id=raw,
                          locator={"line": 1}, snippet="x")
    with store.transaction(db):
        job = ops.start_job(db, project_id=project, kind="ingest",
                            coverage="full", coverage_scope={},
                            parser_fingerprint=None)
        ops.add_observation(
            db, project_id=project, job_id=job, evidence_id=ev, source_id=src,
            entity_key=key, kind="domain", semantic_field="tech", value=value,
            observed_at=at, observed_at_accuracy=accuracy,
            observed_at_source=source, observed_at_precision=precision)


def test_timeline_partitions_by_8_1_accuracy_and_orders_only_ranked(db, project):
    """§8.1 用法约束：exact/approximate 进有序 events；unknown 只进 lower_bounds。

    关键断言：unknown 那条的时间（01-02）**落在** exact（01-01）与 approximate
    （01-03）之间 —— 若把它当 exact 混排，它会插进 events 中间。它必须在
    lower_bounds，证明"没被静默当成可排序时间点"。
    """
    _plant(db, project, key="a.example.com", value="nginx",
           at="2026-01-01T00:00:00Z", accuracy="exact",
           source="manual", precision="second")
    _plant(db, project, key="b.example.com", value="apache",
           at="2026-01-03T00:00:00Z", accuracy="approximate",
           source="file_mtime", precision="day")
    _plant(db, project, key="c.example.com", value="iis",
           at="2026-01-02T00:00:00Z", accuracy="unknown",
           source="tool", precision="unknown")
    store.recompute(db)

    tl = query.timeline(db, project)

    ranked_keys = [e["entity_key"] for e in tl["events"]]
    assert ranked_keys == ["a.example.com", "b.example.com"], \
        f"events 应只含 exact/approximate 且按 observed_at 升序：{ranked_keys}"
    lower_keys = [e["entity_key"] for e in tl["lower_bounds"]]
    assert lower_keys == ["c.example.com"], \
        f"unknown 必须单独进 lower_bounds，不与 exact 交错：{lower_keys}"
    # §8.1 的时间四元组原样搬出，不新造字段
    for e in tl["events"] + tl["lower_bounds"]:
        for col in ("observed_at", "observed_at_source",
                    "observed_at_accuracy", "observed_at_precision"):
            assert col in e, f"时间线事件缺 §8.1 字段 {col}"
    # 取值域是 001 冻结的那套
    assert {e["observed_at_accuracy"] for e in tl["events"]} <= {"exact",
                                                                "approximate"}
    assert all(e["observed_at_accuracy"] == "unknown"
               for e in tl["lower_bounds"])
    assert tl["counts"] == {"ranked": 2, "lower_bounds": 1}
    # unknown 的 lower_bound 标出"不早于"，且明确 rankable=False
    unk = tl["lower_bounds"][0]
    assert unk["rankable"] is False and unk["lower_bound"] == unk["observed_at"]


def test_timeline_goes_into_the_package_with_8_1_semantics(db, project):
    """时间线随包走，manifest/README 把 §8.1 语义带出去（格式与说明随包）。"""
    _plant(db, project, key="a.example.com", value="nginx",
           at="2026-01-01T00:00:00Z", accuracy="exact",
           source="manual", precision="second")
    store.recompute(db)
    files = export.build_package(db, project, full=False,
                                 tool_version="test", project_name="p")
    tl = json.loads(files["timeline.json"].decode("utf-8"))
    assert tl["events"], "包里的 timeline 没有排序事件"
    assert "§8.1" in tl["semantics"], "timeline 没带 §8.1 语义说明"
    manifest = json.loads(files[export.MANIFEST_NAME].decode("utf-8"))
    assert "§8.1" in (manifest.get("semantics") or ""), "manifest 没带时间语义"


# ===========================================================================
# 打包自洽：external 只带路径 + sha256；零外部链接 / 零 file://
# ===========================================================================


def test_external_raw_files_carry_path_and_sha_but_never_body(db, project,
                                                              tmp_path):
    """4.1 红线：external 正文**永不进包**，`--full` 也不例外；只带路径 + sha256。"""
    _ingest(db, project, tmp_path, archive="external")
    for full in (False, True):
        files = export.build_package(db, project, full=full,
                                     tool_version="test", project_name="p")
        raw_index = json.loads(files["raw_files.json"].decode("utf-8"))
        assert raw_index, f"full={full}: raw_files.json 为空"
        for entry in raw_index:
            assert entry["custody"] == "external"
            assert entry["sha256"], f"full={full}: external 缺 sha256"
            assert entry.get("path"), f"full={full}: external 缺路径"
            assert entry["packaged_as"] is None, \
                f"full={full}: external 正文不该进包"
        assert not any(n.startswith("raw/") for n in files), \
            f"full={full}: external 导入不该有 raw/ 正文"


def test_readme_and_manifest_have_no_external_or_absolute_path_links(
        db, project, tmp_path):
    """R7 / R7-ASSETS 的教训搬到包上：说明文件零外部引用、零 file:// 活链接。

    查看者打开 README 不该被动发起任何请求（无 CDN / 外部图 / http 资源），
    也没有指向本机绝对路径的 `file://` 链接。external 的路径是**数据**（JSON
    字段），不是渲染出来的链接。
    """
    _ingest(db, project, tmp_path, archive="external")
    files = export.build_package(db, project, full=False,
                                 tool_version="test", project_name="p")
    for name in (export.README_NAME, export.MANIFEST_NAME):
        text = files[name].decode("utf-8")
        assert "file://" not in text, f"{name} 含 file:// 链接"
        assert "](http://" not in text and "](https://" not in text, \
            f"{name} 含外部 markdown 链接"
        assert "![](http" not in text and "<img" not in text, \
            f"{name} 含外部资源引用（查看者会被动请求）"
        assert "<script" not in text.lower() and "@import" not in text, \
            f"{name} 含会触发请求的引用"


def test_manifest_lists_every_file_with_sha256(db, project, tmp_path):
    """包自描述：manifest 列出每个文件的 sha256 + 字节数（收包的人能验完整性）。"""
    _ingest(db, project, tmp_path)
    files = export.build_package(db, project, full=False,
                                 tool_version="test", project_name="p")
    manifest = json.loads(files[export.MANIFEST_NAME].decode("utf-8"))
    listed = {f["name"] for f in manifest["files"]}
    # manifest 自己不含在内（自己的哈希放不进自己里），其余都该被列到
    assert listed == set(files) - {export.MANIFEST_NAME}, \
        f"manifest 的文件清单与实际包不一致：{listed ^ (set(files) - {export.MANIFEST_NAME})}"
    for f in manifest["files"]:
        assert len(f["sha256"]) == 64 and f["bytes"] >= 0


def test_redact_value_masks_credentials_and_is_identity_on_clean_data():
    """值级掩码的两条性质：凭据形状 → 掩码；干净串 → 恒等（不破坏往返）。

    注意掩码靠的是 `detect_secrets` 的**规则**（authorization: / token= 一族），
    不是"看着像随机串就掩" —— 所以喂给它的必须是真命中规则的形状。
    """
    masked = export.redact_value(f"Authorization: Bearer {CRED_PLAINTEXT}")
    assert CRED_PLAINTEXT not in masked and "****" in masked, masked
    masked2 = export.redact_value(f"token={QUERY_TOKEN}")
    assert QUERY_TOKEN not in masked2 and "****" in masked2, masked2
    # 干净串 / 非字符串 / 空 → 恒等
    assert export.redact_value("app.example.com") == "app.example.com"
    assert export.redact_value(123) == 123
    assert export.redact_value("") == ""
    assert export.redact_value(None) is None


def test_migration_count_unchanged(db):
    """R4：本步不加迁移（读现有表），迁移数与库一致。"""
    applied = query.schema_version(db)
    assert applied == max(m.version for m in migrate.MIGRATIONS)
