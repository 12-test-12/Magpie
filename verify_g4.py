"""G4 出口 Gate：把阶段 4 的两条验收固化成可重复执行的脚本（照 G2 ⑧ / G3 的做法）。

开发实施计划 §4 的 G4 那一行是这两条：

    **明文凭据永不落库（专项断言）；离线指纹有回归夹具。**

两条都要求「能举出反例」—— 一个从没红过的断言不证明任何事（照
`verify_v32r5.py` 的 B3 / B5 写法）。本脚本把它们落成 A/B 两段 + D 段进程内
反例自检 + 两个外部反例开关；C 段确认接线（本脚本在 `check.py` 默认执行里、
两条 pytest 专项也在默认执行里）。

**判据 1：明文凭据永不落库（A 段）—— 扫描是本脚本自己写的，不复用 4.2。**

  复验纪律原文：「判据 1 要自己设计扫描：不要复用 4.2 的实现去验 4.2 的结论
  （那等于让被考的人自己出题）」。所以这里**一行都不 import** `scan` /
  `normalize.detect_secrets` / `redact_secrets`：针（needle）是本脚本写死的
  明文常量，扫描 = 逐格 / 逐字节搜这些常量 —— 掩码逻辑坏没坏，与"拿明文去
  搜"这件事正交。三个落点各一段：

    1. **扫全表** —— sqlite_master 逐表逐格（表 + 视图都扫，比 4.2 自己的
       专项多一层：视图是投影，扫它是零成本的加严）；
    2. **扫报告文件** —— `<data_dir>/reports/**` 逐字节（先断言报告真的落了盘，
       否则 0 命中是空转的 0）；
    3. **扫导出包** —— `export.build_package()` 的**每一个**文件逐字节，默认包
       与 `--full` 包都扫。

  输入是本脚本现场造的三份（头部凭据 / 查询串凭据 / basic 口令 + 一份**文件名
  凭据**），`stored` 与 `external` 两条归档路径各跑一遍。每根针先过**正对照**：
  针必须真的在源文件里、secret 实体必须真的落库且 by_rule 与手数一致 —— 否则
  「0 命中」什么都证明不了。

  三个**边界**（都不是放宽，是把裁定写死，越界即红）：

    * `--full` 包的 `raw/**` 允许带明文 —— 那是 `--full` 的定义（stderr 有告警）；
      断言是「命中只许出现在 raw/，结构化文件永远 0」；
    * external 模式的 `raw_files.locator_uri` **一列**是 LARI-37 裁定的唯一
      DB 例外（指针必须说真话）—— 断言是 ⊆ 这一列**且这一列必须真的带着文件名**
      （例外是活的，不是碰巧没有）；
    * external 模式的导出包 `raw_files.json` 的 `path` 字段 = 同一根指针的
      序列化（`_plain_path(locator_uri)`），本脚本**收窄允许到这一个文件**，
      其余任何文件出现即红。注：内容凭据（头部/查询串/口令）在默认包里是
      **绝对 0**，这一条不让。

**判据 2：离线指纹有回归夹具（B 段）。**

  三问：钉还在不在（expected.json 的 `rules_digest`）、钉是不是活的（等于今天
  实算的 `rules_digest()` —— 改任何一条 `fingerprints/*.yaml` 它必须对不上）、
  钉有没有混进 definition_hash（`definition_extra() == "rules:" + 钉`，否则
  规则变了幂等跳过会吞掉重导）。外加两条接线断言：pytest 的钉测试存在、
  `check.py` 默认跑 `magpie/core/tests`。

**外部反例开关**（与 `MAGPIE_VERIFY_G3_BREAK` 同用途；刻意不碰盘上文件 ——
改真文件再改回来那种反例会在某次跑挂时把仓库留在脏状态）：

    MAGPIE_VERIFY_G4_BREAK=mask   把 4.2 的掩码逻辑改成恒等（进程内 monkeypatch
                                  redact_secrets / mask_secret）→ A 段必须红
    MAGPIE_VERIFY_G4_BREAK=pin    规则内容改一个字节、钉钉死不动 → B 段必须红

亲手做过的真红（一次性证据，写在 G4 结论评论里，不进本脚本）：真改
`normalize.py` → `test_secrets.py` 真红一次；真改 `fingerprints/servers.yaml` →
`test_rules_digest_matches_the_pinned_fixture` 真红一次 —— 各自 git checkout 还原。

分工：本脚本**不替代** pytest —— `test_secrets.py` 的专项是逐向量合同，
`test_fingerprints.py` 的钉测试是逐字节合同；本段是**三落点全覆盖 + 接线 +
反例牙齿**，与 pytest 谁少了都会在出口那一刻被看见。

运行:  py -3.13 verify_g4.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from magpie.core import export, ingest, normalize, ops, store  # noqa: E402
from magpie.core.parsers import _fingerprints, fingerprint      # noqa: E402

WORK = ROOT / "_scratch" / "verify_g4"

# ---------------------------------------------------------------------------
# 四根针。全是**本脚本写死的明文常量** —— 扫描就是拿它们去逐格搜，与 4.2 的
# 掩码实现零耦合（这正是复验纪律要的独立性）。
# ---------------------------------------------------------------------------

#: 内容凭据：头部（authorization_header + github_token 两条规则都会命中）。
NEEDLE_H = "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
#: 内容凭据：URL 查询串（password_param 命中；LARI-37 向量 A 的形状）。
NEEDLE_Q = "G4VerifyQueryNeedle2026"
#: 内容凭据：curl -u 的口令（basic_auth 命中）。
NEEDLE_B = "G4VerifyBasicPass2026"
#: 文件名凭据（github_token 词首不带 \\b，`tok_ghp_…` 粘连形状 —— LARI-37 向量 B）。
NEEDLE_F = "ghp_F4verifyFilenameToken2026ab"

CONTENT_NEEDLES = {"H 头部": NEEDLE_H, "Q 查询串": NEEDLE_Q, "B 口令": NEEDLE_B}
ALL_NEEDLES = dict(CONTENT_NEEDLES, **{"F 文件名": NEEDLE_F})

PASS: list = []

#: 外部反例开关（见模块 docstring）。
BREAK = os.environ.get("MAGPIE_VERIFY_G4_BREAK", "").strip()
_BREAK_VALUES = ("", "mask", "pin")


def check(title, cond, detail=""):
    PASS.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {title}" + (f"  {detail}" if detail else ""))


def note(title, detail=""):
    """报告而不判 —— 一条说清了自己不判的报告，不会训练人忽略红色。"""
    print(f"[note] {title}" + (f"  {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# 本脚本自己的三个扫描器（独立实现；D 段拿构造样本自检它们）
# ---------------------------------------------------------------------------


def scan_cells(conn: sqlite3.Connection, needle: str) -> list:
    """全表逐格搜 needle → [(表, 列), …]（表**和**视图都扫）。

    比 4.2 专项的 `_cells_containing` 多扫视图：视图是投影，一个从表里算不出
    的值不会凭空出现在视图里 —— 但"扫它"是零成本的，加严不解释。

    为什么这里的裸 SQL 不违反 SQL-FREE：那条红线只钉 `cli/read.py` 与
    `api/routes.py`（"读这条路只有一份实现"），而且 query.py 也**不该**有
    "把每张表每格倒出来"这种产品 API —— 这是取证扫描器，不是产品读路径。
    """
    hits = []
    rows = conn.execute(
        "SELECT name, type FROM sqlite_master "
        "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'").fetchall()
    for row in rows:
        name = row["name"] if isinstance(row, sqlite3.Row) else row[0]
        cur = conn.execute(f'SELECT * FROM "{name}"')  # noqa: S608 — 表名来自 sqlite_master
        cols = [d[0] for d in cur.description]
        for record in cur:
            for col, value in zip(cols, record):
                if value is not None and needle in str(value):
                    hits.append((name, col))
    return sorted(set(hits))


def scan_tree_bytes(root: Path, needle: str) -> list:
    """目录树逐字节搜 needle → 命中的相对路径列表（目录不存在 → 空）。"""
    payload = needle.encode("utf-8")
    hits = []
    if not root.is_dir():
        return hits
    for path in sorted(root.rglob("*")):
        if path.is_file() and payload in path.read_bytes():
            hits.append(path.relative_to(root).as_posix())
    return hits


def package_hits(files: dict, needle: str, *, full: bool,
                 allow_pointer: bool = False) -> list:
    """导出包逐字节搜 needle，**带边界地**返回违规文件名。

    边界（越界即返回，见模块 docstring）：
      * `full=True` 时 `raw/**` 合法 —— --full 就是去带原文的；
      * `allow_pointer=True` 时 `raw_files.json` 合法 —— external 模式下它的
        `path` 是 LARI-37 唯一例外那根指针的序列化，**只对文件名针开**，
        内容针（头部/查询串/口令）在任何文件里出现都算违规；
      * 其余任何文件出现 needle = 违规。
    """
    payload = needle.encode("utf-8")
    bad = []
    for name, content in files.items():
        if payload not in content:
            continue
        if full and name.startswith("raw/"):
            continue
        if allow_pointer and name == "raw_files.json":
            continue
        bad.append(name)
    return sorted(bad)


def check_pin(pinned: str, computed: str) -> bool:
    """钉与实算的比较。**独立成函数是为了能自检**（D4）。"""
    return bool(pinned) and pinned == computed


# ---------------------------------------------------------------------------
# 输入：三份现场造的文件（形状照 probe 验过的来；手数写死在 A6）
# ---------------------------------------------------------------------------


def _har_with_credentials() -> dict:
    """单行 HAR：Authorization 头带 H、URL 查询串带 Q（两根针同在一行上）。"""
    return {"log": {"version": "1.2", "creator": {"name": "verify_g4", "version": "1"},
                    "entries": [{
                        "startedDateTime": "2026-09-23T00:00:00.000Z", "time": 5,
                        "request": {"method": "GET",
                                    "url": f"https://api.example.com/v1/me?api_key={NEEDLE_Q}",
                                    "httpVersion": "HTTP/1.1",
                                    "headers": [
                                        {"name": "Content-Type", "value": "application/json"},
                                        {"name": "Authorization",
                                         "value": f"Bearer {NEEDLE_H}"}],
                                    "queryString": [], "headersSize": 0, "bodySize": 0},
                        "response": {"status": 200, "statusText": "OK",
                                     "httpVersion": "HTTP/1.1",
                                     "headers": [{"name": "Content-Type",
                                                  "value": "application/json"}],
                                     "content": {"size": 2,
                                                 "mimeType": "application/json"},
                                     "redirectURL": "", "headersSize": 0,
                                     "bodySize": 2},
                        "cache": {},
                        "timings": {"send": 1, "wait": 2, "receive": 2},
                    }]}}


def _clean_har() -> dict:
    """无凭据内容的最小 HAR —— 文件名带针时，针只可能从文件名这条进来。"""
    doc = _har_with_credentials()
    entry = doc["log"]["entries"][0]
    entry["request"]["url"] = "https://api.example.com/health"
    entry["request"]["headers"] = [{"name": "Accept", "value": "application/json"}]
    return doc


def write_inputs(src: Path) -> dict:
    """写三份输入 → {文件名: 该文件里**应当**存在的针}。"""
    src.mkdir(parents=True, exist_ok=True)
    (src / "cred.har").write_text(json.dumps(_har_with_credentials()),
                                  encoding="utf-8")
    (src / "cmds.curl").write_text(
        f"curl -u alice:{NEEDLE_B} https://a.example.com/x\n"
        f"curl -H \"Authorization: Bearer {NEEDLE_H}\" https://b.example.com/y\n",
        encoding="utf-8")
    (src / f"tok_{NEEDLE_F}.har").write_text(json.dumps(_clean_har()),
                                             encoding="utf-8")
    return {"cred.har": ["H 头部", "Q 查询串"],
            "cmds.curl": ["B 口令", "H 头部"],
            f"tok_{NEEDLE_F}.har": ["F 文件名"]}


# ---------------------------------------------------------------------------
# A · 判据 1：明文凭据永不落库（stored / external 各一遍）
# ---------------------------------------------------------------------------


def _hand_tally(report: dict, expect: dict, label: str) -> list:
    """A6 正对照：报告的 by_rule / secrets / occurrences = **手数**。

    不是"两遍相等"—— 双写 bug 下两遍会相等地错（LARI-37 的教训）。手数在
    probe 里逐文件核过一遍，写死在这里。
    """
    bad = []
    secrets = report.get("secrets") or {}
    if secrets.get("by_rule") != expect["by_rule"]:
        bad.append(f"{label}: by_rule={secrets.get('by_rule')} 手数={expect['by_rule']}")
    for key in ("secrets", "occurrences", "lines"):
        if int(secrets.get(key, -1)) != expect[key]:
            bad.append(f"{label}: {key}={secrets.get(key)} 手数={expect[key]}")
    return bad


def section_a() -> None:
    print()
    print("=== A · 判据 1：明文凭据永不落库（本脚本自己的三落点扫描） ===")
    if BREAK == "mask":
        # 反例开关：把 4.2 的掩码逻辑改成恒等（进程内，不碰盘）。写入口的
        # redact_secrets 与扫描器的 mask_secret 都经模块属性查找 —— patch 模块
        # 属性即可生效。改坏之后 A 段的"0 命中"必须不再成立。
        normalize.redact_secrets = lambda text: text
        normalize.mask_secret = lambda value: ("" if value is None else str(value))
        note("反例开关 MAGPIE_VERIFY_G4_BREAK=mask 已生效：redact/mask 改为恒等 —— "
             "A 段下面应当有 FAIL")

    expected_reports = {
        "cred.har": {"by_rule": {"authorization_header": 1, "github_token": 1,
                                 "password_param": 1},
                     "secrets": 3, "occurrences": 3, "lines": 1},
        "cmds.curl": {"by_rule": {"authorization_header": 1, "github_token": 1,
                                  "basic_auth": 1},
                      "secrets": 3, "occurrences": 3, "lines": 2},
    }

    for mode, archive in (("stored", "archived"), ("external", "external")):
        external = mode == "external"
        data = WORK / mode / "data"
        data.mkdir(parents=True, exist_ok=True)
        reports = data / "reports"
        conn = store.connect(":memory:")
        store.init_db(conn)
        pid = ops.create_project(conn, f"g4-{mode}", {"include": [], "exclude": []})
        src = WORK / mode / "src"
        plan = write_inputs(src)

        # --- 逐份导入（report_dir 打开：落盘报告是落点 2 的被扫对象） ---
        tally_bad, states = [], []
        for name in plan:
            report = ingest.ingest_file(conn, ingest.IngestOptions(
                project_id=pid, data_dir=data, actor="verify_g4", verify=True,
                archive=archive, report_dir=reports), src / name)
            states.append(report.get("state"))
            if name in expected_reports:
                tally_bad.extend(_hand_tally(report, expected_reports[name], name))
        check(f"A1 [{mode}] 三份输入 state 全 done（cred/curl 的报告手数逐项相等）",
              states == ["done", "done", "done"] and not tally_bad,
              f"states={states} {'；'.join(tally_bad)}")

        # --- 正对照：针真的在源文件里（否则 0 命中是空转的 0） ---
        missing = [label for label, needle in ALL_NEEDLES.items()
                   if not any(needle.encode() in p.read_bytes()
                              for p in src.iterdir() if p.is_file())
                   and label != "F 文件名"]
        fn_needle_present = f"tok_{NEEDLE_F}.har" in [p.name for p in src.iterdir()]
        if not fn_needle_present:
            missing.append("F 文件名")
        check("A2 正对照：四根针都真的在源文件里（0 命中必须非空转）",
              not missing, f"缺：{missing}")

        # --- 落点 1：全表逐格（表 + 视图） ---
        db_bad = []
        for label, needle in CONTENT_NEEDLES.items():
            cells = scan_cells(conn, needle)
            if cells:
                db_bad.append(f"{label}→{cells}")
        f_cells = scan_cells(conn, NEEDLE_F)
        if external:
            # LARI-37 裁定：唯一例外 = raw_files.locator_uri 一列；且例外必须是
            # 活的 —— 指针真的带着文件名，"0 命中"反而说明指针被掩坏了。
            allowed = [("raw_files", "locator_uri")]
            if sorted(set(f_cells)) != allowed:
                db_bad.append(f"F 文件名→{sorted(set(f_cells))}（应恰好={allowed}）")
        elif f_cells:
            db_bad.append(f"F 文件名→{f_cells}（stored 模式应 0 —— 归档是内容寻址的）")
        check(f"A3 [{mode}] 落点1 全表逐格（表+视图）：内容凭据 0；文件名针"
              f"{'⊆ 裁定例外列且例外是活的' if external else ' 0'}",
              not db_bad, "；".join(db_bad))

        # --- 落点 2：落盘报告逐字节（先证明报告真的存在） ---
        report_files = [p for p in reports.rglob("*") if p.is_file()] \
            if reports.is_dir() else []
        rep_bad = []
        if len(report_files) < len(plan):
            rep_bad.append(f"报告只有 {len(report_files)} 份 < 输入 {len(plan)} 份")
        for label, needle in ALL_NEEDLES.items():
            hits = scan_tree_bytes(reports, needle)
            if hits:
                rep_bad.append(f"{label}→{hits}")
        check(f"A4 [{mode}] 落点2 落盘报告逐字节：四根针 0（且报告非空转）",
              not rep_bad, "；".join(rep_bad))

        # --- 落点 3：导出包（默认包 + --full 包都扫；判定统一走 package_hits，
        #    D3 自检的就是这个生产函数） ---
        pkg_bad = []
        default_pkg = export.build_package(conn, pid, full=False,
                                           tool_version="verify_g4",
                                           project_name=f"g4-{mode}")
        for label, needle in CONTENT_NEEDLES.items():
            # 内容凭据在默认包里**绝对 0** —— 一个都不许有，raw_files.json 也不行
            # （allow_pointer 只对文件名针开，见 package_hits 的边界）。
            hits = package_hits(default_pkg, needle, full=False, allow_pointer=False)
            if hits:
                pkg_bad.append(f"默认包 {label}→{hits}")
        f_default = [n for n, body in default_pkg.items()
                     if NEEDLE_F.encode() in body]
        if external:
            # 比 ⊆ 更强：**必须恰好在** raw_files.json —— 指针例外是活的
            # （path 字段真的带着文件名），且别的文件一个都没有。
            if f_default != ["raw_files.json"]:
                pkg_bad.append(f"默认包 F 文件名→{f_default}（应恰好=[raw_files.json]，"
                               f"它是 locator_uri 指针的序列化）")
        elif f_default:
            pkg_bad.append(f"默认包 F 文件名→{f_default}（stored 模式应 0）")
        check(f"A5a [{mode}] 落点3 默认导出包逐字节：内容凭据全文件 0；文件名针"
              f"{'恰好只在 raw_files.json（指针序列化，且必须在）' if external else ' 0'}",
              not pkg_bad, "；".join(pkg_bad))

        full_pkg = export.build_package(conn, pid, full=True,
                                        tool_version="verify_g4",
                                        project_name=f"g4-{mode}")
        full_bad = []
        if external and any(n.startswith("raw/") for n in full_pkg):
            full_bad.append("external 的 --full 包出现了 raw/ 正文（4.1 红线）")
        for label, needle in CONTENT_NEEDLES.items():
            hits = package_hits(full_pkg, needle, full=True, allow_pointer=False)
            if hits:
                full_bad.append(f"--full 结构化文件 {label}→{hits}")
        if not external:
            raw_bodies = [n for n in full_pkg if n.startswith("raw/")]
            if not raw_bodies:
                full_bad.append("--full 包一个 raw/ 正文都没有（包是空转的？）")
            elif not any(NEEDLE_H.encode() in full_pkg[n] for n in raw_bodies):
                # 正对照：raw/ 在、且真的带出了针 —— 否则"命中只许在 raw/"是
                # 一句空转的 0。
                full_bad.append("raw/ 正文在、但里面找不到 H —— 正对照失败")
        f_hits = package_hits(full_pkg, NEEDLE_F, full=True, allow_pointer=external)
        if f_hits:
            full_bad.append(f"--full 文件名针→{f_hits}")
        check(f"A5b [{mode}] 落点3 --full 包：命中只许在 raw/"
              f"{'（external 连 raw/ 都不该有）' if external else '，结构化文件恒 0'}"
              f"；raw/ 正对照非空转",
              not full_bad, "；".join(full_bad))

        # --- A6 手数：secret 实体 4 个、出现 6 次（按键去重；不是"两遍相等"） ---
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM entities WHERE kind='secret'"
            " AND is_current=1").fetchone()
        occ = 0
        for rep in sorted(reports.glob("*.json")):
            secrets = (json.loads(rep.read_text(encoding="utf-8")).get("secrets")
                       or {})
            occ += int(secrets.get("occurrences") or 0)
        check(f"A6 [{mode}] secret 实体手数=4、出现手数=6（cred 3 + curl 3，"
              f"H 的两个键跨文件复用）",
              int(row["n"]) == 4 and occ == 6,
              f"实体={row['n']} 出现={occ}")
        conn.close()
    note("A 段手数依据：cred.har 单行 → {authorization_header:1, github_token:1, "
         "password_param:1}（3 实体）；cmds.curl 两行 → {authorization_header:1, "
         "github_token:1, basic_auth:1}（3 实体，其中 H 的两个键与 cred.har 复用）")


# ---------------------------------------------------------------------------
# B · 判据 2：离线指纹有回归夹具
# ---------------------------------------------------------------------------


def section_b() -> None:
    print()
    print("=== B · 判据 2：离线指纹有回归夹具（钉还在 / 钉是活的 / 钉进 hash） ===")
    case = ROOT / "magpie" / "core" / "tests" / "fixtures" / "fingerprint" / "1"
    expected_path = case / "expected.json"

    pinned = None
    if expected_path.is_file():
        try:
            pinned = json.loads(expected_path.read_text(encoding="utf-8")).get(
                "rules_digest")
        except (OSError, json.JSONDecodeError) as exc:
            check("B1 钉文件读得出 rules_digest", False, str(exc))
    check("B1 回归夹具的钉还在：expected.json 里有 rules_digest（sha256: 前缀）",
          isinstance(pinned, str) and pinned.startswith("sha256:"),
          f"pinned={pinned!r}")

    computed = _fingerprints.rules_digest()
    if BREAK == "pin":
        # 反例开关：规则内容改一个字节、**钉钉死不动** → 比较必须红。
        # （copy 到工作区改，不碰盘上的 fingerprints/*.yaml。）
        doctor = WORK / "rules_doctored"
        shutil.rmtree(doctor, ignore_errors=True)
        doctor.mkdir(parents=True, exist_ok=True)
        for path in sorted(_fingerprints.RULES_DIR.glob("*.yaml")):
            shutil.copy(path, doctor / path.name)
        victim = sorted(doctor.glob("*.yaml"))[0]
        victim.write_text(victim.read_text(encoding="utf-8") + "\n# 反例：改一个字节\n",
                          encoding="utf-8")
        computed = _fingerprints.rules_digest(doctor)
        note(f"反例开关 MAGPIE_VERIFY_G4_BREAK=pin 已生效：{victim.name} 改了一个字节、"
             f"钉钉死不动 —— B2/B3 下面应当有 FAIL")
    check("B2 钉是活的：rules_digest() 实算 == 钉（改任何一条规则这一条必须红）",
          check_pin(str(pinned), computed),
          f"pinned={pinned} computed={computed}")

    extra = fingerprint.FingerprintParser.definition_extra()
    check("B3 钉混进了 definition_hash（definition_extra == 'rules:'+钉 —— "
          "否则规则变了、幂等跳过会吞掉重导）",
          check_pin(f"rules:{pinned}", extra) if BREAK != "pin" else
          check_pin(str(pinned), computed),   # BREAK=pin 时这里同 B2 一起红
          f"extra={extra!r}")

    # 接线：钉的 pytest 测试存在（它跑在默认执行里 —— C 段核 check.py 那一半）。
    tf = (ROOT / "magpie" / "core" / "tests" / "test_fingerprints.py")
    source = tf.read_text(encoding="utf-8") if tf.is_file() else ""
    check("B4 钉的 pytest 专项还在：test_rules_digest_matches_the_pinned_fixture",
          "def test_rules_digest_matches_the_pinned_fixture" in source,
          tf.name if tf.is_file() else "文件不存在")

    # 正对照：钉不是随便一个字符串 —— 今天的实算（未开反例时）必须非空且带前缀。
    if BREAK != "pin":
        check("B5 正对照：今天的 rules_digest() 真实可算（sha256: 前缀非空）",
              isinstance(computed, str) and computed.startswith("sha256:")
              and len(computed) > 10, f"computed={computed}")


# ---------------------------------------------------------------------------
# C · 接线：本脚本与两条 pytest 专项都在 check.py 默认执行里
# ---------------------------------------------------------------------------


def section_c() -> None:
    print()
    print("=== C · 接线（谁少了都会在出口那一刻被看见） ===")
    text = (ROOT / "check.py").read_text(encoding="utf-8")
    check("C1 verify_g4.py 在 check.py 的 UNIT 执行表里（本 Gate 默认就跑）",
          '"verify_g4.py"' in text)
    check("C2 check.py 默认跑 pytest magpie/core/tests（test_secrets 专项 + "
          "test_fingerprints 钉测试都在里面）",
          '"magpie/core/tests"' in text)
    ts = ROOT / "magpie" / "core" / "tests" / "test_secrets.py"
    source = ts.read_text(encoding="utf-8") if ts.is_file() else ""
    check("C3 判据 1 的 pytest 专项还在：明文永不落库 + 两条向量 + external 裁定用例",
          all(name in source for name in (
              "def test_plaintext_never_reaches_the_database_stored",
              "def test_plaintext_never_reaches_the_database_external",
              "def test_url_query_credential_never_reaches_the_database",
              "def test_filename_credential_external_mode_only_in_raw_locator")),
          ts.name if ts.is_file() else "文件不存在")


# ---------------------------------------------------------------------------
# D · 反例自检：每个扫描器 / 比较函数都拿构造样本验一遍牙齿 + 对照
# ---------------------------------------------------------------------------


def section_d() -> None:
    print()
    print("=== D · 反例自检（造一个反例就必须红；再造对照证明不误报） ===")

    # --- D1 全表扫描器 ---
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t(v TEXT)")
    conn.execute("CREATE VIEW v AS SELECT v FROM t")
    conn.execute("INSERT INTO t VALUES ('干净值')")
    check("D1 对照：干净库里全表扫描器 0 命中", not scan_cells(conn, NEEDLE_H))
    conn.execute("INSERT INTO t VALUES (?)", (f"x{NEEDLE_H}y",))
    hit_tables = {t for t, _ in scan_cells(conn, NEEDLE_H)}
    check("D1 反例①：表里埋一根针 → 必须红（且视图这条投影路也扫得到）",
          hit_tables == {"t", "v"}, f"命中表={sorted(hit_tables)}")
    conn.close()

    # --- D2 报告字节扫描器 ---
    probe = WORK / "d_probe"
    shutil.rmtree(probe, ignore_errors=True)
    probe.mkdir(parents=True)
    (probe / "clean.json").write_text('{"ok": true}', encoding="utf-8")
    check("D2 对照：干净目录 0 命中", not scan_tree_bytes(probe, NEEDLE_Q))
    (probe / "leak.json").write_text(f'{{"path": "{NEEDLE_Q}"}}', encoding="utf-8")
    check("D2 反例②：报告里埋一根针 → 必须红",
          scan_tree_bytes(probe, NEEDLE_Q) == ["leak.json"])
    shutil.rmtree(probe, ignore_errors=True)

    # --- D3 导出包扫描器的四条边界 ---
    pack = {"entities.json": b"clean", "raw_files.json": b"clean",
            "raw/deadbeef": NEEDLE_H.encode()}
    check("D3 反例①：默认包（full=False）里 raw/ 带针 → 必须红"
          "（默认包不许带正文）",
          package_hits(pack, NEEDLE_H, full=False) == ["raw/deadbeef"])
    check("D3 对照①：--full 包里同一根针在 raw/ → 合法不报（--full 的定义）",
          package_hits(pack, NEEDLE_H, full=True) == [])
    pack_struct = {"secrets.json": f'"{NEEDLE_H}"'.encode()}
    check("D3 反例②：针出现在结构化文件 → 必须红（--full 也救不了它）",
          package_hits(pack_struct, NEEDLE_H, full=True)
          == ["secrets.json"])
    pack_ptr = {"raw_files.json": f'"{NEEDLE_F}"'.encode()}
    check("D3 对照②：文件名针在 external 的 raw_files.json（指针序列化，"
          "调用方开 allow_pointer）→ 合法；不开 → 必须红",
          package_hits(pack_ptr, NEEDLE_F, full=False, allow_pointer=True) == [] and
          package_hits(pack_ptr, NEEDLE_F, full=False) == ["raw_files.json"])
    check("D3 反例③：内容针落进 raw_files.json（调用方给内容针绝不开指针豁免）"
          "→ 必须红 —— 指针豁免只属于文件名针",
          package_hits({"raw_files.json": f'"{NEEDLE_H}"'.encode()},
                       NEEDLE_H, full=False, allow_pointer=False)
          == ["raw_files.json"])

    # --- D4 钉比较器 ---
    check("D4 反例④：钉与实算不等 → 必须红", not check_pin("sha256:aa", "sha256:bb"))
    check("D4 对照④：钉与实算相等 → 不报", check_pin("sha256:aa", "sha256:aa"))
    check("D4 反例⑤：钉是空串/None → 必须红（钉丢了不许静默过）",
          not check_pin("", "") and not check_pin(None, None))

    # --- D5 掩码独立性的正对照：needle 的掩码形态不等于 needle 本身 ---
    masked = normalize.mask_secret(NEEDLE_H)
    check("D5 对照：针的掩码形态（前4+****）不含针本体 —— "
          "A 段拿明文搜不会被掩码形态误触发，也不会漏搜",
          NEEDLE_H not in masked and masked.endswith("****"),
          f"masked={masked!r}")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    if BREAK not in _BREAK_VALUES:
        print(f"未知的反例开关 MAGPIE_VERIFY_G4_BREAK={BREAK!r}"
              f"（可选：{'/'.join(v for v in _BREAK_VALUES if v)}）")
        return 1

    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)

    section_a()
    section_b()
    section_c()
    section_d()

    print()
    if BREAK:
        print(f"!! 反例开关已打开: MAGPIE_VERIFY_G4_BREAK={BREAK}"
              " —— 上面应当有 FAIL（否则这个开关是装饰）")
    print(f"结果: {sum(PASS)}/{len(PASS)} 通过")
    return 0 if all(PASS) else 1


if __name__ == "__main__":
    sys.exit(main())
