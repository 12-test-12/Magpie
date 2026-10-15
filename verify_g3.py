"""G3 出口 Gate：把阶段 3 的三条验收固化成可重复执行的脚本（照 G2 ⑧ 的做法）。

开发实施计划 §4 那一行是这三条：

    **每个新增来源有夹具；`kind` 三件套齐全；无性能退化。**

三条里有两条可以在**秒级**内被机器判定，这个脚本负责它们；第三条（性能）
是一条**测量**，它的入口是 `check.py bench`（严格模式），本脚本只负责确认
那个入口还在、且还是严格的（见 C 段）——把一次三分钟的导入塞进默认跑的
脚本里，等于让「每次改一行都要跑一遍」这件事不再成立，而 G2 已经为此付过
一次代价（`verify_v32r5.py` 里那一段贵过 6 秒的用例是刻意挑出来的）。

三条验收各自怎么落到断言上（以及**为什么不是别的方式**）
----------------------------------------------------------

**① 每个新增来源有夹具（黄金 + 负向）。**

  关键词是「每个」。所以来源清单**从发现机制算出来**（`ingest.load_parsers()`），
  不是手抄一张表 —— 手抄的表在新增第 13 个解析器的那天**不会红**，它只会
  静默地少检一个。这里反过来：只要某个解析器没有黄金夹具、或者没有对应的
  负向载荷，本脚本当场红。新增一个来源因此**必须**同时带来两样东西。

  负向载荷是**本脚本自己写的**（不是把 pytest 里那张表 import 过来重跑一遍）：
  那 12 份载荷的形状各不相同（截断的 JSON / 缺 `template-id` 的 nuclei 行 /
  v1 的 Postman 集合 / 没有 `log` 的 HAR …），判据是「零记录 + 一句人话」。

**② `kind` 三件套齐全。**

  **逐行**核 `kind_registry`：身份规则非空、镜头归属在冻结的六个之内、
  `fixture_path` 指向的文件真的存在，并且那个文件里的**每条样例都要被声明的
  身份规则真的算成声明的 key**（`examples`），**每条反例都要真的被拒**
  （`rejects`）。这三样缺一样，「样例夹具」就退化成一句描述。

  另外两处**此前没有任何人校验过**的声明，本脚本顺手也管了（都是 0 成本的）：

    * 夹具里的 `golden` 字段 —— 它声明「哪几个黄金夹具在演这个 kind」，
      但全仓**没有任何读者**（在 `magpie/` 下 grep 这个键是空的）。一条没人读
      的声明等于一句注释，而它恰好是可验证的：每个条目指向的夹具必须存在，
      且**真的解析出**这个 kind。
    * `produced_by` —— 它声明「谁在产出这个 kind」。既有测试只要求「某个夹具里
      出现过它」，本脚本要求**声明里的那个来源自己**的黄金夹具里出现它。
      ⚠️ 这条只对**阶段 3 的四个 kind** 判红（见 `_STRICT_PRODUCER_KINDS` 的
      说明），老的 kind 只报告不判。

**③ 无性能退化。**

  是一条**测量**，不是一条静态事实 —— 数字由 `py -3.13 check.py bench` 产出，
  结论（含 CPU 速度尺与「空载 / 争用」两个数）写在 G3 的结论评论里。本脚本能做
  且该做的是另一件事：确认那个入口**还在、还是严格的**（`MAGPIE_PERF_STRICT=1`
  必须真的被设上、四条 bench 必须还在执行表里）。一个被悄悄摘掉的性能入口
  和「性能达标」长得一模一样。

自带反例自检（每一条断言都要能举出「造一个反例它就红」的证据）
------------------------------------------------------------
照 `verify_v32r5.py` 的 B3 / B5 写法：**一个从没红过的断言不证明任何事。**
D 段把上面每一条断言都喂一份**做过手脚的输入**，要求它必须判红，再喂一份
干净的作对照（防止假阳性）。做法与 `check.py` 的 `_readonly_audit()` 相同：
检查逻辑是**纯函数**（不读盘），生产调用递真实数据、自检递构造出来的样本 ——
验的是同一段代码。这样自检不会把仓库留在脏状态（改一个真文件再改回来那种
自检，会在某次跑挂时留下一个被改过的夹具）。

分工：本脚本**不替代** pytest
------------------------------
`magpie/core/tests/test_parsers.py` 的黄金用例是**逐字段合同**（改动任何一个
字段就红，报错直接指出第几条的第几个字段），负向用例是那一套固定的边界清单。
本脚本是**覆盖面 Gate**：它盯的是「有没有漏掉一整个来源」和「登记表里的声明
是不是真的」，这两件事 pytest 都看不见（它们的形状是「少一条」而不是「错一条」）。

两者都在 `check.py` 默认执行里，谁少了都会在出口那一刻被看见。

运行:  py -3.13 verify_g3.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from magpie.core import ingest, normalize, ops, query, scan, store  # noqa: E402
from magpie.core.parsers import base, domainlist, httpx           # noqa: E402

FIXTURES = ROOT / "magpie" / "core" / "tests" / "fixtures"

#: `kind_registry.fixture_path` 是**相对 `magpie/core/tests/`** 写的
#: （`fixtures/kinds/<kind>.json`）—— 那是测试目录里的路径，不是仓库根的。
#: 锚点写错的话每一条都会报「文件不存在」，而那种红看起来跟真的缺文件一样。
TESTS_DIR = FIXTURES.parent

#: 工作区：可重复运行，每次清空重建。`_scratch/` 不入库（见 .gitignore），
#: 也不在 `check.py` 的静态红线扫描范围内。
WORK = ROOT / "_scratch" / "verify_g3"

#: 阶段 2 就有的两个来源。**写死是对的**：它是一次显式的确认，与
#: `test_load_parsers_finds_every_parser_and_retires_the_bridge` 里那张写死的
#: 清单同一条纪律 —— 多出来的解析器一律算作「阶段 3 的新来源」，于是它必须
#: 带齐夹具与负向载荷，否则本脚本红。**这正是 G3-① 想要的形状。**
_STAGE2_TOOLS = frozenset({"domainlist", "httpx"})

#: 阶段 3 新增的四个 kind。它们走 `produced_by` 的**严格**判据（见 B 段）。
#: 老的 kind（domain / ip / host / service / product）只报告不判红，理由是实测：
#: 那 8 个 3.3 / 3.4 的来源在**带 IP 字面量**的输入下真的会产出 `ip`
#: （2026-09-22 亲测 8/8），只是它们的黄金夹具恰好全是域名主机 ——
#: 那是**夹具覆盖面的窄**，不是 `produced_by` 说了假话。把夹具变窄判成 Gate
#: 不通过，会逼着人为了凑绿去改夹具，而 G3 要回答的不是这个。
_STRICT_PRODUCER_KINDS = frozenset({"cidr", "vuln", "endpoint", "apischema"})

#: 夹具里那条身份规则能引用的函数（白名单，不是 `eval`）。与
#: `test_parsers.py::_identity_rules()` 同一份口径：夹具是**数据**，不该有执行
#: 任意表达式的权力。这里另列一份而不是 import 测试模块 —— 一个 Gate 脚本
#: 依赖被测代码的测试模块，等于让「测试自己坏掉」和「产品坏掉」长得一模一样。
_RULES = {
    "normalize.normalize_domain": normalize.normalize_domain,
    "normalize.normalize_ip": normalize.normalize_ip,
    "normalize.normalize_cidr": normalize.normalize_cidr,
    "normalize.product_name": normalize.product_name,
    "normalize.version_tuple": normalize.version_tuple,
    "normalize.normalize_vuln_target": normalize.normalize_vuln_target,
    "normalize.vuln_key": normalize.vuln_key,
    "normalize.endpoint_key": normalize.endpoint_key,
    "normalize.apischema_key": normalize.apischema_key,
    # 4.1 的 flow 身份规则（登记在 `kind_registry.identity_rule` 里）。
    # 与 `test_parsers.py::_identity_rules()` 的白名单同一份口径。
    "normalize.flow_key": normalize.flow_key,
    # 4.2 的 secret 身份规则与它的掩码（同样登记在 `kind_registry.identity_rule`）。
    "scan.secret_key": scan.secret_key,
    "normalize.mask_secret": normalize.mask_secret,
    "normalize.param_name": normalize.param_name,
    "httpx.service_key": httpx.service_key,
    "httpx.product_key": httpx.product_key,
    "domainlist.accepts": domainlist.accepts,
    "identity.literal": lambda *args: (args[0] if isinstance(args[0], str)
                                       and args[0] else None),
}

PASS: list = []

#: 反例的**外部**开关（与 `verify_v32r5.py` 的 `MAGPIE_VERIFY_V32R5_BREAK` 同一个
#: 用途，但这里要证明的是**覆盖面**这条线有牙齿）：
#:
#:     MAGPIE_VERIFY_G3_BREAK=fixture   某个来源的黄金夹具没了      → A 段红
#:     MAGPIE_VERIFY_G3_BREAK=negative  某个来源的负向载荷没了      → A 段红
#:     MAGPIE_VERIFY_G3_BREAK=kind      某个 kind 夹具的样例改错    → B 段红
#:     MAGPIE_VERIFY_G3_BREAK=producer  登记表里多一个假产出者      → B 段红
#:
#: D 段的自检证明的是**判据本身**（纯函数）咬得住；这个开关证明的是**接线**：
#: 判据红不红，得看真实数据真的流进它。一条"改数据也不报警"的断言，和一个
#: 永远绿的断言没有区别 —— 而它更危险，因为它看起来验过一遍了。
#:
#: 刻意**不碰盘上的文件**：改真文件再改回来那种反例，会在某次跑挂时把仓库
#: 留在脏状态。这里的四处手脚全在内存里。
BREAK = os.environ.get("MAGPIE_VERIFY_G3_BREAK", "").strip()
_BREAK_VALUES = ("", "fixture", "negative", "kind", "producer")


def check(title, cond, detail=""):
    PASS.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {title}" + (f"  {detail}" if detail else ""))


def note(title, detail=""):
    """**报告而不判**：一条值得看见、但今天不构成 Gate 不通过的事实。

    这个项目里「报告而不判」是有位置的（`bench` 的 CPU 速度尺就是），前提是
    别说成断言 —— 一条永远绿的断言比没有更糟，一条说清了自己不判的报告不会。
    """
    print(f"[note] {title}" + (f"  {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# 共用零件：把夹具与解析结果折成同一套视图
# ---------------------------------------------------------------------------


def encode(record) -> dict:
    """`Raw*` → 与 `expected.json` 同构的 dict（与 test_parsers 同口径）。

    只为本脚本的「夹具是不是活的」那一问服务 —— 它是**子集**判据，不是逐字段
    合同（逐字段合同在 pytest 那边，见模块 docstring 的分工一节）。
    """
    if isinstance(record, base.RawObservation):
        return {"type": "observation", "entity_ref": list(record.entity_ref),
                "field": record.type, "value": record.value}
    if isinstance(record, base.RawRelation):
        return {"type": "relation", "src": list(record.src), "dst": list(record.dst),
                "rel": record.rel}
    return {"type": "entity", "kind": record.kind, "key": record.key}


def _freeze(value):
    """比较用的规范化：dict → 排序后的元组，list → 元组，其余取字符串。"""
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return str(value)


def _key_of(item: dict) -> tuple:
    return _freeze(encode_like(item))


def encode_like(item: dict) -> dict:
    """`expected.json` 里的一条记录 → `encode()` 的那套字段。

    `expected.json` 多带了 `display` / `subtitle` / `attrs` 这些**展示**列，
    而 `encode()` 只取身份那几件。夹具的逐字段比对在 pytest 里，这里只问
    「这条事实还在不在」，所以两边都收敛到同一个最小形状。
    """
    if item.get("type") == "observation":
        return {"type": "observation", "entity_ref": list(item["entity_ref"]),
                "field": item["field"], "value": item["value"]}
    if item.get("type") == "relation":
        return {"type": "relation", "src": list(item["src"]),
                "dst": list(item["dst"]), "rel": item["rel"]}
    return {"type": "entity", "kind": item["kind"], "key": item["key"]}


def declared_of(expected: dict) -> set:
    return {_key_of(item) for item in expected.get("records") or []}


def load_fixture(path: Path):
    """读一份**夹具** JSON。`BREAK=kind` 时在返回前把一条样例改错。

    单独一个读取点是为了让"做手脚"只有一处落点 —— 四处各读各的，就会有
    一处漏掉、于是反例开关只生效一半，而那比没有开关更糟。
    """
    doc = json.loads(path.read_text(encoding="utf-8"))
    if BREAK == "kind" and doc.get("examples"):
        doc["examples"][0]["key"] = "被反例开关改过的 key"
    return doc


def parse_records(cls, lines: list) -> tuple:
    """跑一遍解析器 → (记录, ctx)。**不读盘**，行由调用方给。"""
    ctx = base.OfflineContext()
    return list(cls().parse(lines, ctx)), ctx


def kinds_of(records) -> set:
    return {r.kind for r in records if isinstance(r, base.RawEntity)}


def rels_of(records) -> set:
    return {r.rel for r in records if isinstance(r, base.RawRelation)}


# ---------------------------------------------------------------------------
# 纯检查函数（可注入数据 → 可自检；生产调用递真实扫描结果）
# ---------------------------------------------------------------------------


class SourceFacts:
    """一个来源在「夹具覆盖面」这一问上的全部事实。

    **构造出来也能用** —— 自检就是这么做的（D 段的样本全是现场造的）。
    """

    def __init__(self, tool, golden=None, expected=None, records=None,
                 error="", negatives=None):
        self.tool = tool
        self.golden = golden              # 黄金夹具目录（None = 不存在）
        self.expected = expected          # expected.json 的内容
        self.records = records            # 解析结果（None = 没跑成）
        self.error = error
        self.negatives = negatives or []  # 负向载荷（条数就是覆盖面）


def audit_source(facts: SourceFacts) -> list:
    """一个来源的覆盖面 → 问题列表。判红的每一问都对应 G3-① 的一句话。"""
    bad = []
    tool = facts.tool

    if facts.golden is None:
        bad.append(f"{tool}: 没有黄金夹具目录 fixtures/{tool}/<format_version>/ "
                   f"—— G3-①：每个新增来源都必须带黄金夹具")
    elif not (facts.golden / "expected.json").is_file():
        bad.append(f"{tool}: 黄金夹具目录里没有 expected.json —— 没有它就没有"
                   f"「当时被确认过的输出」，夹具退化成一份输入文件")
    elif not isinstance(facts.expected, dict):
        bad.append(f"{tool}: expected.json 读不出一个对象")
    else:
        if str(facts.expected.get("tool")) != tool:
            bad.append(f"{tool}: expected.json 的 tool 写的是 "
                       f"{facts.expected.get('tool')!r} —— 夹具与来源对不上")
        declared = declared_of(facts.expected)
        if not declared:
            bad.append(f"{tool}: expected.json 里一条记录都没有 —— 空夹具不算夹具")
        if facts.records is None:
            bad.append(f"{tool}: 夹具跑不起来（{facts.error or '解析失败'}）")
        else:
            # **夹具是活的**：声明的每条事实今天仍能被解析器算出来。
            # 这条判的是「子集」而不是「相等」—— 相等是 pytest 的逐字段合同，
            # 而 Gate 要回答的是另一问：这份夹具是不是已经成了死文件。
            actual = {_freeze(encode(r)) for r in facts.records}
            missing = declared - actual
            if missing:
                sample = sorted(missing)[:2]
                bad.append(f"{tool}: 夹具里声明的 {len(missing)} 条事实现在算不出来 "
                           f"—— 夹具已经死了（举例：{sample}）")

    if not facts.negatives:
        bad.append(f"{tool}: 没有负向载荷 —— G3-①：每个新增来源都必须带负向夹具")
    return bad


def audit_negative(tool: str, payload: str, records, bad_lines) -> list:
    """一条负向载荷 → 问题列表。判据是 2.4 那张表里写死的两条：

        | 负向夹具 | 每一条都**不崩**，且产出可读的坏行报告（不是 Python 堆栈） |

    「不崩」在这里的可检验形式是**零记录 + 有坏行 + 坏行理由里没有堆栈** ——
    三条缺一不可：只判「没抛异常」的话，一个静默吞掉整份输入的解析器会全绿。
    """
    bad = []
    # 载荷片段进每一条报错：一条只说"某条载荷坏了"的报告，读的人还得回去翻
    # 是哪一条 —— 而这里的载荷是**一行一行写在源码里的常量**，指出来最省事。
    excerpt = " ".join(str(payload).split())[:60]
    if records:
        bad.append(f"{tool}: 负向载荷落下了 {len(records)} 条记录 —— "
                   f"坏输入必须一条事实都不落（载荷：{excerpt!r}）")
    if not bad_lines:
        bad.append(f"{tool}: 负向载荷没有产出任何坏行 —— 没有坏行报告，"
                   f"使用者只会看到「这份文件没东西」（载荷：{excerpt!r}）")
    for line in bad_lines:
        reason = str(line.get("reason", ""))
        if "Traceback" in reason or not reason.strip():
            bad.append(f"{tool}: 坏行理由是堆栈或空 —— {reason[:80]!r}")
    return bad


def audit_kind(row: dict, doc, *, rules, golden_kinds, real_tools,
               strict: bool) -> tuple:
    """kind_registry 的一行 + 它的夹具 → (问题, 提示)。

    `golden_kinds` 是 {"<tool>/<fmt>": {kind, …}}，由**真的跑过解析器**算出来，
    不是从夹具里读的 —— 后者是「声明」，前者才是「事实」，而这一整段的用途
    正是拿事实去核声明。

    `strict` 决定 `produced_by` 走严判据还是只提示，理由见模块顶部 ②。
    """
    problems: list = []
    notes: list = []
    kind = str(row["kind"])

    if not str(row["identity_rule"]).strip():
        problems.append(f"{kind}: 缺身份规则 —— 三件套的第一件")
    if str(row["lens"]) not in base.LENSES:
        problems.append(f"{kind}: 镜头归属 {row['lens']!r} 不在 §3.5 冻结的六个之内 "
                        f"（{sorted(base.LENSES)}）")

    if doc is None:
        problems.append(f"{kind}: fixture_path 指向的文件不存在或读不出 —— "
                        f"三件套的第二件（样例夹具）没有落点")
        return problems, notes

    if doc.get("kind") != kind:
        problems.append(f"{kind}: 夹具里写的 kind 是 {doc.get('kind')!r}")
    if doc.get("lens") != row["lens"]:
        problems.append(f"{kind}: 镜头归属两处不一致 —— 登记表 {row['lens']!r} / "
                        f"夹具 {doc.get('lens')!r}")
    if doc.get("identity_rule") != row["identity_rule"]:
        problems.append(f"{kind}: 身份规则两处不一致（登记表 vs 夹具）—— "
                        f"两份描述一旦分叉，「哪个才是身份规则」就没有答案了")

    # --- 样例必须**可执行**：真的被声明的规则算成声明的 key ---
    examples = doc.get("examples") or []
    if not examples:
        problems.append(f"{kind}: 夹具里没有样例 —— 一句描述的夹具不算夹具")
    for example in examples:
        rule = rules.get(str(example.get("rule")))
        if rule is None:
            problems.append(f"{kind}: 夹具引用了白名单之外的身份规则 "
                            f"{example.get('rule')!r} —— 它无法被执行，"
                            f"于是这条样例永远绿")
            continue
        try:
            got = rule(*example.get("args", []))
        except Exception as exc:                                  # noqa: BLE001
            problems.append(f"{kind}: 样例 {example.get('args')} 求值就抛了 "
                            f"{type(exc).__name__}: {exc}")
            continue
        if got != example.get("key"):
            problems.append(f"{kind}: 样例算不出声明的 key —— "
                            f"{example.get('args')} → {got!r}，声明的是 "
                            f"{example.get('key')!r}")

    # --- 反例必须**真的被拒** ---
    rejects = doc.get("rejects") or []
    if not rejects:
        problems.append(f"{kind}: 夹具里一条反例都没有 —— 一条不会拒绝任何东西的"
                        f"身份规则，与「没有规则」无法区分")
    for reject in rejects:
        rule = rules.get(str(reject.get("rule")))
        if rule is None:
            problems.append(f"{kind}: 反例引用了白名单之外的身份规则 "
                            f"{reject.get('rule')!r}")
            continue
        try:
            outcome = rule(*reject.get("args", []))
        except (normalize.NormalizeError, TypeError):
            continue                       # 抛错 = 被拒（契约里的两种形状）
        except Exception as exc:                                  # noqa: BLE001
            problems.append(f"{kind}: 反例抛了非契约异常 {type(exc).__name__}: {exc}")
            continue
        if outcome:
            problems.append(f"{kind}: 反例**没有被拒** —— {reject.get('args')} 得到了 "
                            f"{outcome!r}，而它是作为「必须被拒」写进夹具的")

    # --- `golden` 字段：一条此前无人校验的声明 ---
    for entry in doc.get("golden") or []:
        if "/" not in str(entry):
            problems.append(f"{kind}: golden 里的 {entry!r} 不是 <tool>/<format> 形状")
        elif entry not in golden_kinds:
            problems.append(f"{kind}: golden 指向的黄金夹具 {entry} 不存在 —— "
                            f"这条声明指不到任何东西")
        elif kind not in golden_kinds[entry]:
            problems.append(f"{kind}: golden 声称 {entry} 在演它，但那份夹具里一个 "
                            f"{kind} 实体都没有（实测：{sorted(golden_kinds[entry])}）")

    # --- produced_by：谁在产出它 ---
    for producer in row.get("produced_by") or []:
        name = str(producer)
        tool = name.split("@", 1)[0]
        if tool not in real_tools:
            problems.append(f"{kind}: produced_by 里的 {name!r} 不是任何一个已发现的"
                            f"解析器 —— 一句指不到东西的声明")
            continue
        # 声明的产出者，自己的黄金夹具里必须真的出现这个 kind。
        hit = any(kind in ks for entry, ks in golden_kinds.items()
                  if entry.split("/", 1)[0] == tool)
        if not hit:
            message = (f"{kind}: produced_by 声明 {name} 产出它，但那份来源的黄金"
                       f"夹具里没有 {kind} 实体")
            (problems if strict else notes).append(message)
    return problems, notes


# ---------------------------------------------------------------------------
# 真实数据的装配
# ---------------------------------------------------------------------------


def golden_kinds_by_fixture(parsers: dict) -> dict:
    """{"<tool>/<fmt>": 实测产出的 kind 集合} —— 跑解析器，不读声明。"""
    out = {}
    for tool, cls in parsers.items():
        case = FIXTURES / tool / str(cls.format_version)
        expected_path = case / "expected.json"
        if not expected_path.is_file():
            continue
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        text = (case / expected["input"]).read_text(encoding="utf-8")
        records, _ctx = parse_records(cls, text.splitlines())
        out[f"{tool}/{cls.format_version}"] = kinds_of(records)
    return out


def gather_sources(parsers: dict, negatives: dict) -> dict:
    """每个来源 → SourceFacts（黄金夹具 + 活证据 + 负向载荷条数）。"""
    out = {}
    for tool, cls in parsers.items():
        case = FIXTURES / tool / str(cls.format_version)
        facts = SourceFacts(tool, golden=case if case.is_dir() else None,
                            negatives=negatives.get(tool) or [])
        if BREAK == "fixture" and tool == "nmap":
            facts.golden = None            # 反例：一个来源的黄金夹具"没了"
        if facts.golden is not None and (case / "expected.json").is_file():
            try:
                expected = json.loads((case / "expected.json").read_text(encoding="utf-8"))
                text = (case / expected["input"]).read_text(encoding="utf-8")
                facts.expected = expected
                facts.records, _ctx = parse_records(cls, text.splitlines())
            except Exception as exc:                              # noqa: BLE001
                facts.error = f"{type(exc).__name__}: {exc}"
        out[tool] = facts
    return out


def kind_rows(conn) -> list:
    """登记表逐行 —— **走 `query.kinds()`**，不自己写一条 SELECT。

    读这条路只有一份实现（`check.py` 的 SQL-FREE 红线守着 3.5 的那次收敛）；
    G3 的复验脚本没有理由成为那个例外，否则「GUI 与 CLI 给出同一个答案」这句
    话在**验收工具**这一层就已经不成立了。
    """
    rows = query.kinds(conn)
    if BREAK == "producer":
        for row in rows:                   # 反例：登记表里多一个指不到东西的产出者
            if row["kind"] == "cidr":
                row["produced_by"] = list(row["produced_by"]) + ["nosuchtool@1"]
    return rows


# ---------------------------------------------------------------------------
# A · 每个来源都有夹具（黄金 + 负向）
# ---------------------------------------------------------------------------

#: 每个来源一份**必须被拒**的载荷：形状各自贴着自己格式最脆的那一处。
#: 判据（`audit_negative`）：零记录 + 至少一条坏行 + 理由不是堆栈。
NEGATIVES = {
    "domainlist": ["http://app.example.com/x\n..\nnot a domain\n"],
    "httpx": ['{"host": "app.example.com", "tech": [}\n'],
    "nmap": ['<?xml version="1.0"?>\n<nmaprun><host><status state=\n'],
    "nuclei": ['{"host": "https://a.example.com"}\n'],
    "gau": ["https:/app.example.com/admin\n://nope\n"],
    "katana": ['{"request": {"method": "GET", "endpo\n'],
    "dirsearch": ["[12:00:01] 200 -  123B  - \n"],
    # ffuf 这一条**形状完好、内容坏掉**（results 里那条 url 不是 URL）：它同时
    # 是「被认领的」（sniff 0.95），所以它也走得到仲裁那条路上。而「顶层有
    # results 但没有 config」的 JSON 是另一件事 —— 那份在 pytest 里
    # （`test_endpoint_ffuf_report_without_config_is_not_adopted`）。
    "ffuf": [json.dumps({"config": {"url": "http://a.example.com/FUZZ"},
                         "results": [{"input": {"FUZZ": "x"}, "status": 200,
                                      "url": "not a url"}]})],
    "openapi": ['{"openapi": "3.0.3", "paths": \n'],
    "postman": [json.dumps({"info": {"name": "Old", "_postman_id": "x", "schema":
                                     "https://schema.getpostman.com/json/collection"
                                     "/v1.0.0/collection.json"}, "item": []})],
    "har": ['{"entries": []}\n'],
    # tshark 这一条是**截断的导出**（数组没闭合）：它是这份格式在真实
    # 世界里最容易出现的坏形状（`tshark` 写到一半被 Ctrl-C / 磁盘满了）。
    "tshark": ['[{"_index": "x", "_source": {"layers": {"ip_ip_src": "10.0.0.5"'],
    "curl": ["this is not a curl command\nnor is this\n"],
    # fingerprint 这一条**形状完好、内容拒**：请求段与响应段都在，但 origin-form
    # 请求行 + Host 不带端口 —— 80 还是 443 无从知道，引擎报可读坏行拒绝
    # （与 flow 的"两端都未知直接拒绝"同一条纪律），而不是猜一个 service。
    "fingerprint": ["GET /admin HTTP/1.1\nHost: app.example.com\n"
                    "\nHTTP/1.1 200 OK\nServer: nginx/1.24.0\n\n<html></html>\n"],
}


def build_parsers() -> dict:
    """发现机制给出的解析器清单 → {tool: 类}。

    `source` 必须是包扫描那条路 —— 它同时证明「新增一个解析器文件就够了」
    这件事还成立（3.1 起就靠它，没有别处的登记表要改）。
    """
    parsers, source = ingest.load_parsers()
    mapping = {cls.tool: cls for cls in parsers}
    if source != "magpie.core.parsers" or not mapping:
        raise SystemExit(f"发现机制失效：source={source!r}，{len(mapping)} 个解析器 "
                         f"—— 覆盖面 Gate 无从谈起")
    return mapping


def section_a(parsers: dict) -> None:
    print()
    print("=== A · 每个来源都有夹具（黄金 + 负向） ===")

    facts_by_tool = gather_sources(parsers, NEGATIVES)
    stage3 = sorted(set(parsers) - _STAGE2_TOOLS)

    # 这两个数**每加一个来源就要改一次**，这是刻意的：它们的用途是"发现机制还
    # 活着吗、来源清单有没有多出没被看见的一个"。4.1 加了 `tshark`（12→13 /
    # 10→11），4.3 加了 `fingerprint`（13→14 / 11→12），改这一行是一次
    # **显式的确认** —— 与 `test_load_parsers_finds_
    # every_parser_and_retires_the_bridge` 里那份写死的清单同一条纪律。
    check(f"A1 发现机制给出 {len(parsers)} 个解析器，其中阶段 3 起新增 {len(stage3)} 个"
          f"（清单从代码算出来，不是手抄的）",
          len(parsers) == 14 and len(stage3) == 12,
          f"全部={sorted(parsers)}；阶段 3={stage3}")

    problems = []
    for tool in sorted(facts_by_tool):
        problems.extend(audit_source(facts_by_tool[tool]))
    check("A2 每个来源都带黄金夹具，且夹具是**活的**（声明的每条事实今天仍算得出来）",
          not problems, "；".join(problems))

    # 逐来源把数字摆出来 —— 结论要能被复算，就得先看得见每一个来源。
    for tool in sorted(facts_by_tool):
        facts = facts_by_tool[tool]
        declared = len((facts.expected or {}).get("records") or [])
        got = len(facts.records or [])
        bad_lines = len((facts.expected or {}).get("bad_lines") or [])
        print(f"     {tool:11s} 黄金：声明 {declared:3d} 条 / 实算 {got:3d} 条"
              f"（夹具内坏行 {bad_lines}）；负向载荷 {len(facts.negatives)} 条")

    # --- 负向载荷逐条真跑 ---
    neg_problems = []
    for tool in sorted(parsers):
        for index, payload in enumerate(NEGATIVES.get(tool) or []):
            text = payload if isinstance(payload, str) else json.dumps(payload)
            records, ctx = parse_records(parsers[tool], text.splitlines())
            neg_problems.extend(
                f"[{tool} #{index + 1}] {p}" for p in
                audit_negative(tool, text, records, ctx.bad_lines))
    check("A3 每条负向载荷都真的零记录 + 有坏行 + 理由不是堆栈（12 个来源逐个跑）",
          not neg_problems, "；".join(neg_problems))

    _ingest_negatives(sorted(facts_by_tool))


def _ingest_negatives(tools: list) -> None:
    """一小组负向载荷走完整 `ingest`：**逐来源强制指定解析器**跑一遍。

    为什么强行指定：这条要问的是「**这个来源**的解析器，在完整管线里遇到坏输入
    会不会崩、会不会凭空落事实」，而不是「仲裁会不会把它判给别人」（那是 §5.4 的
    另一条线，pytest 里有用例）。指定之后每一轮都真的经过探测层、归档、对账、
    收尾不变量校验 —— 崩在那一段和崩在解析器里是两件事。
    """
    shutil.rmtree(WORK / "neg", ignore_errors=True)
    data_dir = WORK / "neg" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    payloads = {"empty": b"", "binary": b"PK\x03\x04" + b"\x00" * 64 + b"zip"}
    problems = []
    for tool in tools:
        conn = store.connect(":memory:")
        store.init_db(conn)
        try:
            for name, blob in payloads.items():
                path = WORK / "neg" / f"{tool}-{name}.dat"
                path.write_bytes(blob)
                project = ops.create_project(conn, f"g3-{tool}-{name}",
                                             {"include": [], "exclude": []})
                options = ingest.IngestOptions(
                    project_id=project, data_dir=data_dir, parser=tool,
                    source_name=tool, archive="external")
                try:
                    report = ingest.ingest_file(conn, options, path)
                except Exception as exc:                          # noqa: BLE001
                    problems.append(f"{tool}/{name}: 管线抛了 "
                                    f"{type(exc).__name__}: {exc}")
                    continue
                dumped = json.dumps(report, ensure_ascii=False, default=str)
                counts = report.get("counts") or {}
                if "Traceback" in dumped:
                    problems.append(f"{tool}/{name}: 报告里漏出了 Python 堆栈")
                if report.get("state") not in ("done", "partial", "skipped", "failed",
                                               "needs_confirmation", "cancelled"):
                    problems.append(f"{tool}/{name}: 状态 {report.get('state')!r} 不在"
                                    f"约定的那几个里")
                if int(counts.get("records") or 0) != 0:
                    problems.append(f"{tool}/{name}: 坏输入落下了 "
                                    f"{counts.get('records')} 条记录")
                if not (report.get("reason") or report.get("skip_reason")
                        or (report.get("encoding") or {}).get("notes")):
                    problems.append(f"{tool}/{name}: 既没成功也没给理由")
        finally:
            conn.close()
    check(f"A4 空文件与二进制走完整管线（{len(tools)} 个来源 × 2 种输入）：不崩、"
          f"零事实、有可读理由", not problems, "；".join(problems[:6]))


# ---------------------------------------------------------------------------
# B · kind 三件套（逐行核）
# ---------------------------------------------------------------------------


def section_b(parsers: dict, conn) -> None:
    print()
    print("=== B · kind 三件套齐全（逐行核 kind_registry）===")

    rows = kind_rows(conn)
    golden_kinds = golden_kinds_by_fixture(parsers)
    real_tools = set(parsers)

    problems, notes = [], []
    for row in rows:
        kind = str(row["kind"])
        raw_path = str(row["fixture_path"])
        fixture = TESTS_DIR / raw_path
        doc = None
        if not raw_path.startswith("fixtures/"):
            # 指向 fixtures/ 之外等于这个条目**不参与扫描**，白名单式的空转；
            # 夹具只可能住在那个目录里，写到别处一定是错的。
            problems.append(f"{kind}: fixture_path {raw_path!r} 不在 fixtures/ 下 —— "
                            f"它指向的地方不属于夹具目录")
        elif not fixture.is_file():
            problems.append(f"{kind}: fixture_path 指向的文件不存在 —— {raw_path}")
        else:
            try:
                doc = load_fixture(fixture)
            except (OSError, json.JSONDecodeError) as exc:
                problems.append(f"{kind}: 夹具读不了 —— {exc}")
        strict = kind in _STRICT_PRODUCER_KINDS
        found, found_notes = audit_kind(row, doc, rules=_RULES,
                                        golden_kinds=golden_kinds,
                                        real_tools=real_tools, strict=strict)
        problems.extend(found)
        notes.extend(found_notes)
        examples = len((doc or {}).get("examples") or [])
        rejects = len((doc or {}).get("rejects") or [])
        print(f"     {kind:11s} lens={row['lens']:9s} 样例 {examples:2d} 条 / "
              f"反例 {rejects:2d} 条 / golden {len((doc or {}).get('golden') or [])} 个 / "
              f"produced_by {len(row['produced_by'])} 个"
              f"{'（严格判据）' if strict else ''}")

    # 同 A1：这个数每加一个 kind 就要改一次（4.1 加了 `flow`，9 → 10；
    # 4.2 加了 `secret`，10 → 11）。改这一行是一次**显式的确认**：一个 kind
    # 落地必须被看见，而"多出来的那一行有没有三件套"正是本段要核的事。
    check(f"B1 {len(rows)} 个 kind 逐行核：身份规则 / 镜头归属 / fixture_path 存在 / "
          f"样例算得出 key / 反例真的被拒 / golden 声明为真 / produced_by 指得到东西",
          len(rows) == 11 and not problems, "；".join(problems))

    for line in notes:
        note("produced_by 缺夹具证据（老 kind，只报告不判红）", line)

    # 关系动词：阶段 3 新增的三个必须有真实产出者（「若干关系动词」那一半）。
    verbs = {v["verb"]: v for v in query.relation_verbs(conn)}
    emitted: dict = {}
    for tool, cls in parsers.items():
        case = FIXTURES / tool / str(cls.format_version)
        expected_path = case / "expected.json"
        if not expected_path.is_file():
            continue
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        text = (case / expected["input"]).read_text(encoding="utf-8")
        records, _ctx = parse_records(cls, text.splitlines())
        for rel in rels_of(records):
            emitted.setdefault(rel, set()).add(tool)
    verbs_bad = []
    for rel in sorted(emitted):
        if rel not in verbs:
            verbs_bad.append(f"{rel}: 被解析器产出，却没在 relation_vocabulary 里登记")
    # ⚠️ 这里列的是**每一个**必须有真实产出者的关系动词（不是"阶段 3 那三个"）：
    # 4.1 的 `involves` 也进来 —— 一条登记了却没有任何来源产出它的动词，与
    # "没有这条动词"在库里无法区分。
    for verb in ("found_on", "served_by", "declared_in", "involves"):
        if verb not in verbs:
            verbs_bad.append(f"{verb}: 阶段 3 登记的关系动词不见了")
        elif not emitted.get(verb):
            verbs_bad.append(f"{verb}: 登记了，但没有任何来源的黄金夹具产出它")
        elif not verbs[verb].get("inverse"):
            verbs_bad.append(f"{verb}: 没有 inverse —— 反向问题要另立一列才答得出")
    check(f"B2 关系动词：这四个都登记了、有逆动词、且真的有来源产出它"
          f"（实测产出 {len(emitted)} 个动词）", not verbs_bad, "；".join(verbs_bad))
    print("     产出关系动词的来源：" + "，".join(
        f"{rel}←{'/'.join(sorted(srcs))}" for rel, srcs in sorted(emitted.items())))


# ---------------------------------------------------------------------------
# C · 性能契约的入口还在（数字在结论评论里）
# ---------------------------------------------------------------------------

#: `check.py` 里 BENCH 那一段要找的文件。写在脚本里而不是从 check.py 解析出来：
#: 「执行表里还有没有这四条」正是这一问本身。
_BENCH_FILES = ("bench/test_perf.py", "bench/test_perf_parse.py",
                "bench/test_perf_ingest.py", "bench/test_perf_app.py")


def section_c(parsers: dict) -> None:
    print()
    print("=== C · 性能契约的入口还在，而且还是严格的 ===")

    text = check_source()
    missing = [name for name in _BENCH_FILES if name not in _bench_block(text)]
    check("C1 四条性能用例仍在 `check.py bench` 的执行表里", not missing,
          f"缺失：{missing}")

    # 严格模式：`MAGPIE_PERF_STRICT=1` 是 §9.2 那些承诺**唯一可验证的形式**。
    # 它一旦没被设上，bench 会退化成「只报告」—— 而「只报告」的绿与「达标」的
    # 绿在 check.py 的输出里长得一模一样。这一条专防那个静默降级。
    check("C2 `check.py bench` 仍然会设上 MAGPIE_PERF_STRICT=1（否则断言全部"
          "退化成报告）", _sets_strict_mode(text), "")

    # 报告而不判：哪些来源没有解析吞吐用例。G3 的三条都没要求「每个来源都有
    # bench」，但这件事值得被看见 —— 它是阶段 4 的来源继续变多时的成本。
    covered = {tool for tool in parsers if f'("{tool}",' in parse_bench_source()}
    uncovered = sorted(set(parsers) - covered)
    note(f"解析吞吐 bench 覆盖 {len(covered)}/{len(parsers)} 个来源",
         f"未覆盖：{uncovered}（G3 未要求，记录在案）")


def check_source() -> str:
    """`check.py` 的源码文本。**只读文件、不 import** ——
    import check.py 会在导入期跑掉 `_declared_floor()` 之类的副作用，
    而一个验收脚本不该顺手执行被验收的入口。"""
    return (ROOT / "check.py").read_text(encoding="utf-8")


def _bench_block(text: str) -> str:
    start = text.find("BENCH = [")
    return text[start:start + 2000] if start >= 0 else ""


def _sets_strict_mode(text: str = None) -> bool:
    """严格模式真的被设上了吗？**两个条件**：那句话在，且它在 bench 的路上。

    第二条不是多余的：把它写进一个跑不到的 `if` 分支里，文本上一样能 grep 到，
    而 bench 会静默退化成「只报告」。
    """
    if text is None:
        text = check_source()
    if 'os.environ["MAGPIE_PERF_STRICT"] = "1"' not in text:
        return False
    for marker in ('if what == "bench":', 'if what == "all":'):
        index = text.find(marker)
        if index < 0 or "MAGPIE_PERF_STRICT" not in text[index:index + 400]:
            return False
    return True


def parse_bench_source() -> str:
    return (ROOT / "bench" / "test_perf_parse.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# D · 反例自检：每一条断言都要能举出「造一个反例它就红」的证据
# ---------------------------------------------------------------------------


def section_d(parsers: dict, conn) -> None:
    print()
    print("=== D · 反例自检（造一个反例就必须红；再造对照证明不误报）===")

    # --- ① 覆盖面：缺夹具 / 死的夹具 / 缺负向 ---
    problems = audit_source(SourceFacts("somesource"))
    check("D1 反例①：一个没有黄金夹具的来源必须被判红",
          any("没有黄金夹具" in p for p in problems), "；".join(problems))

    dead = SourceFacts("t", golden=FIXTURES / "gau" / "1",
                       expected={"tool": "t", "records": [
                           {"type": "entity", "kind": "domain",
                            "key": "never-ever.example.com"}]},
                       records=[], negatives=["x"])
    problems = audit_source(dead)
    check("D2 反例②：夹具声明的事实今天算不出来时必须被判红（夹具已经死了）",
          any("夹具已经死了" in p for p in problems), "；".join(problems))

    problems = audit_source(SourceFacts("t", golden=FIXTURES / "gau" / "1",
                                        expected={"tool": "t", "records": []},
                                        records=[], negatives=[]))
    check("D3 反例③：空夹具与没有负向载荷都必须被判红",
          any("空夹具不算夹具" in p for p in problems)
          and any("没有负向载荷" in p for p in problems), "；".join(problems))

    problems = audit_negative("t", "x", records=[object()], bad_lines=[])
    check("D4 反例④：负向载荷落下了记录、又没有坏行时必须被判红",
          len(problems) == 2, "；".join(problems))

    problems = audit_negative("t", "x", records=[], bad_lines=[
        {"reason": "Traceback (most recent call last)"}])
    check("D5 反例⑤：坏行理由里漏出 Python 堆栈时必须被判红",
          any("堆栈" in p for p in problems), "；".join(problems))

    check("D6 对照：零记录 + 一句人话的负向结果**不得**被误报",
          not audit_negative("t", "x", records=[],
                             bad_lines=[{"reason": "不是一条绝对 URL"}]))

    # --- ② kind 三件套 ---
    rows = {str(r["kind"]): r for r in kind_rows(conn)}
    golden_kinds = golden_kinds_by_fixture(parsers)
    base_args = dict(rules=_RULES, golden_kinds=golden_kinds,
                     real_tools=set(parsers))
    row = rows["cidr"]
    doc = json.loads((FIXTURES / "kinds" / "cidr.json").read_text(encoding="utf-8"))

    problems, _notes = audit_kind(row, doc, strict=True, **base_args)
    check("D7 对照：真实的 cidr 那一行 + 它的夹具必须**全绿**（不自证假阳性）",
          not problems, "；".join(problems))

    doctored = json.loads(json.dumps(doc))
    doctored["examples"][1]["key"] = "10.0.0.7/24"
    problems, _notes = audit_kind(row, doctored, strict=True, **base_args)
    check("D8 反例⑥：把一条样例的 key 改错 → 必须报「算不出声明的 key」",
          any("算不出声明的 key" in p for p in problems), "；".join(problems))

    doctored = json.loads(json.dumps(doc))
    doctored["rejects"][0] = {"rule": "identity.literal", "args": ["10.0.0.7"],
                              "why": "假反例：这条其实不会被拒"}
    problems, _notes = audit_kind(row, doctored, strict=True, **base_args)
    check("D9 反例⑦：一条**其实没被拒**的反例必须被判红（夹具在装样子）",
          any("没有被拒" in p for p in problems), "；".join(problems))

    doctored = json.loads(json.dumps(doc))
    doctored["golden"] = ["httpx/1"]
    problems, _notes = audit_kind(row, doctored, strict=True, **base_args)
    check("D10 反例⑧：golden 指向一份**不产出该 kind** 的夹具必须被判红"
          "（这条声明此前无人校验）",
          any("实体都没有" in p for p in problems), "；".join(problems))

    doctored = json.loads(json.dumps(doc))
    doctored["golden"] = ["nmap/9"]
    problems, _notes = audit_kind(row, doctored, strict=True, **base_args)
    check("D11 反例⑨：golden 指向一个不存在的夹具必须被判红",
          any("不存在" in p for p in problems), "；".join(problems))

    doctored = json.loads(json.dumps(doc))
    doctored["lens"] = "endpoints"
    problems, _notes = audit_kind(row, doctored, strict=True, **base_args)
    check("D12 反例⑩：镜头归属两处不一致（夹具说 endpoints、登记表说 assets）"
          "必须被判红", any("两处不一致" in p for p in problems), "；".join(problems))

    doctored = json.loads(json.dumps(doc))
    doctored["examples"][0] = {"rule": "normalize.made_up", "args": ["x"], "key": "x"}
    problems, _notes = audit_kind(row, doctored, strict=True, **base_args)
    check("D13 反例⑪：夹具引用了白名单之外的规则名必须被判红"
          "（否则它永远绿、也没有任何身份规则在跑）",
          any("白名单之外" in p for p in problems), "；".join(problems))

    doctored_row = dict(row)
    doctored_row["produced_by"] = ["nosuchtool@1"]
    problems, _notes = audit_kind(doctored_row, doc, strict=True, **base_args)
    check("D14 反例⑫：produced_by 里写一个不存在的工具必须被判红",
          any("不是任何一个已发现的解析器" in p for p in problems), "；".join(problems))

    doctored_row = dict(row)
    doctored_row["produced_by"] = ["httpx@1"]
    problems, _notes = audit_kind(doctored_row, doc, strict=True, **base_args)
    check("D15 反例⑬：严格判据下，声明 httpx 产出 cidr（它的夹具里没有 cidr）"
          "必须被判红", any("没有 cidr 实体" in p for p in problems), "；".join(problems))

    problems, notes = audit_kind(doctored_row, doc, strict=False, **base_args)
    check("D16 对照：同一条声明在非严格判据下只进提示、不判红（老 kind 的口径）",
          not problems and bool(notes), f"notes={notes}")

    # --- ③ 性能入口 ---
    # 样本里**没有** bench 那条路，严格模式那句话写在一个跑不到的地方 ——
    # C2 的判据必须说「不严格」（否则它只是在 grep 一个字符串）。
    sample = ('def main():\n'
              '    if what == "lint":\n'
              '        os.environ["MAGPIE_PERF_STRICT"] = "1"\n'
              '        return run(OTHER, "别的什么")\n')
    check("D17 反例⑭：严格模式写在跑不到的分支里时，C2 的判据必须说「不严格」",
          not _sets_strict_mode(sample))
    check("D18 对照：真实的 check.py 必须通过同一个判据（D17 不是靠"
          "「什么都不认」通过的）", _sets_strict_mode())


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    if BREAK not in _BREAK_VALUES:
        print(f"未知的反例开关 MAGPIE_VERIFY_G3_BREAK={BREAK!r}"
              f"（可选：{'/'.join(v for v in _BREAK_VALUES if v)}）")
        return 1
    if BREAK == "negative":
        NEGATIVES.pop("nuclei", None)      # 反例：一个来源的负向载荷"没了"

    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)

    parsers = build_parsers()
    conn = store.connect(":memory:")
    store.init_db(conn)
    try:
        section_a(parsers)
        section_b(parsers, conn)
        section_c(parsers)
        section_d(parsers, conn)
    finally:
        conn.close()

    print()
    if BREAK:
        print(f"!! 反例开关已打开: MAGPIE_VERIFY_G3_BREAK={BREAK}"
              " —— 上面应当有 FAIL（否则这个开关是装饰）")
    print(f"结果: {sum(PASS)}/{len(PASS)} 通过")
    return 0 if all(PASS) else 1


if __name__ == "__main__":
    sys.exit(main())
