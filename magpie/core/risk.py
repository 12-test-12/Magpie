"""风险评分与攻击面概览 —— **纯读侧派生**（5.5 / LARI-45）。

这个模块只做一件事：把库里**已经有**的事实读成「分数 + 为什么」。输入全部
来自已落库的观测 / 实体 / 关系 / Inbox，输出是总分、逐因子分解、以及同一份
事实的另一面 —— 攻击面概览（暴露面按 host / service / endpoint 聚合）。

四条设计纪律，每条都是本步判据的一部分：

1. **零出站（R7）**。评分只吃已进库的事实 —— 不查 CVE 库、不查威胁情报。
   分数因数据不足而保守是**如实**，不是缺陷（`unknown` 档的权重就是为这类
   "知道有、但不知道多严重"留的）。模块里没有一句出站代码；行为断言见
   `core/tests/test_risk.py` 的 socket 三点钉死。

2. **确定性**。同库同输入两次评分逐字节相同：不读 `now()`、不引入随机、
   每一处遍历都有显式排序（`ORDER BY id` / 规则表插入序）。将来若要"时间
   衰减"，输入必须是观测自己的时间（`observed_at`），绝不许读当下时刻 ——
   那会让同一份库在两个时刻给出两个分数，回归夹具从此不可信。

3. **黑盒总分是失败模式**。每个因子带 `count / weight / contribution` 与
   `evidence`（真实存在的实体 / 观测 / Inbox id），`sum(contribution) == total`
   是测试钉住的恒等式 —— 一个解释不了 total 的因子表等于没有因子表。

4. **规则表版本化**。`RULES` 是唯一事实来源，`rules_digest()` 把它钉成 sha256；
   输出同时带 `rule_version`（人读）与 `rules_digest`（机器读）。改规则 =
   改 digest = 回归夹具（`fixtures/risk/expected.json`）当场红，然后**带着
   改动意图**更新夹具 —— 照 4.3 指纹规则的纪律：改坏一次、夹具确实红了、再修绿。

**攻击面概览与评分共用同一次 `collect()`**：两边吃的都是同一份 facts（同一批
漏洞行、同一批服务行），不造第二套统计 —— "概览说 3、评分说 4"这种分歧在结构
上不可能出现。`overview()` 是唯一的组装入口，CLI / API / GUI 三个消费者都只调它。

不做什么（写在明处，免得下一个人以为是漏了）：

  * **不物化**：不加表、不加迁移、不在导入时把分数存起来 —— 评分永远是读出来的；
  * **不联网**：见纪律 1；
  * **不引第三方**（R6：标准库 only）；
  * **不给"绝对安全"背书**：分数只反映库里的事实，"没测过"按 `untested`
    因子显式计分，而不是被当成 0 风险。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Iterable

from . import normalize
from . import query

# 0.19.0：下面这两个"从 key 取 host"的尺子随域名归属推导一起下沉到
# `normalize.py`（`query.under_domain_ids` 要用同一把尺子，而 risk 已 import
# query —— 反向 import 会成环）。旧名在这里**保号**：本模块的调用点与既有
# 测试看到的仍是 `_service_host` / `_endpoint_host`。
_service_host = normalize.service_host
_endpoint_host = normalize.endpoint_host

__all__ = [
    "RULE_VERSION", "RULES", "EVIDENCE_CAP",
    "rules_digest", "factor_ids", "factor_label",
    "collect", "score", "attack_surface", "overview",
]

#: 规则表版本。**改 `RULES` 必须同步加一** —— 它是"这次分数是哪一版规则算的"
#: 的人读答案；`rules_digest` 是同一件事的机器读答案（两者都进输出）。
RULE_VERSION = "1"

#: 每个因子最多列几条依据。**count 不截断、evidence 才截断**：一个 3 万实体
#: 的库把 3 万条 id 塞进 JSON 只会让信封没人读得动，而 `evidence_total` 已经
#: 说清"一共多少条"。截断只影响展开的列表，不影响任何算术。
EVIDENCE_CAP = 50

#: 因子规则表（唯一事实来源）。键的形状就是因子 id 的形状：
#:
#:   * 表 → 表（`vuln_open` / `inbox_open`）：每个键各是一个因子，id = `组.键`；
#:   * 标量（`untested` / `out_of_scope` / …）：id = 组名。
#:
#: 插入序即因子输出序（Python dict 保序）—— 确定性的一部分。
#:
#: 权重怎么定的（能答"为什么是这个数"）：全部是**相对量级**，不是行业标准分 ——
#: critical 100 / high 40 / medium 15 / low 5 意味着"一条 open 的 critical 约等于
#: 两条多 high"，而一条越界资产（8）约等于小半条 medium。这些数字的唯一职责是
#: 把"改一条事实，分数朝哪个方向、动多少"变成可断言的；它们不代表可利用性
#: —— 任何试图让分数"更准"的联网冲动都被纪律 1 挡住。
RULES: dict[str, Any] = {
    "vuln_open": {"critical": 100, "high": 40, "medium": 15,
                  "low": 5, "info": 1, "unknown": 2},
    "inbox_open": {"high": 10, "medium": 5, "low": 2},
    "untested": 3,
    "out_of_scope": 8,
    "exposed_service": 2,
    "exposed_endpoint": 1,
    "secret": 25,
}

#: 词表（与 `parsers/nuclei.SEVERITIES` 同一份取值；多抄一份在这里是为了让
#: core 不反向依赖解析器 —— 出现词表外的 severity 一律落 `unknown` 档）。
SEVERITIES = ("critical", "high", "medium", "low", "info", "unknown")

#: Inbox 的 severity 词表（001 的 CHECK：low / medium / high）。
INBOX_SEVERITIES = ("high", "medium", "low")

#: 因子的人读名。**只在这里出现一次** —— CLI 的 `--human`、GUI 的因子行、
#: 测试的断言都从这里取，三处各写一份中文迟早会漂。
_LABELS: dict[str, str] = {
    "untested": "未测覆盖（零观测实体）",
    "out_of_scope": "越界资产（scope 之外）",
    "exposed_service": "暴露的服务（service 实体）",
    "exposed_endpoint": "暴露的接口（endpoint 实体）",
    "secret": "库内敏感串（secret 实体）",
}

#: SQL 的分批绑定大小。SQLite 变量上限（旧版 999、新版 32766）与集合大小无关，
#: 取 500 在两代实现上都安全（与 `query._CHUNK` 同一个理由，但这里是独立取值：
#: 那个常量是私有的，跨模块引用它会把两处的演化绑死）。
_CHUNK = 500


# ---------------------------------------------------------------------------
# 规则表的指纹（照 4.3 离线指纹的纪律：钉在夹具里，改规则必红）
# ---------------------------------------------------------------------------


def rules_digest() -> str:
    """`RULES` 的 sha256。任何一条权重变了，这个值就变 —— 夹具里的钉立刻不等。

    `sort_keys=True`：摘要只关心"规则的内容"，不关心字面书写顺序（哪天把两组
    对调位置不该让夹具红 —— 红的应该是**语义**变了）。
    """
    payload = json.dumps(RULES, ensure_ascii=True, sort_keys=True,
                         separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def factor_ids() -> list[str]:
    """全部因子 id，按 `RULES` 的插入序展开。输出顺序的唯一来源。"""
    ids: list[str] = []
    for group, rule in RULES.items():
        if isinstance(rule, dict):
            ids.extend(f"{group}.{key}" for key in rule)
        else:
            ids.append(str(group))
    return ids


def factor_label(factor_id: str) -> str:
    """因子 id → 人读名。表规则的子因子（`vuln_open.critical`）现拼后缀。"""
    if factor_id in _LABELS:
        return _LABELS[factor_id]
    group, _, key = factor_id.partition(".")
    if group == "vuln_open":
        return f"未处置漏洞（{key}）"
    if group == "inbox_open":
        return f"未决 Inbox（{key}）"
    return factor_id


def _weight_of(factor_id: str) -> int:
    group, _, key = factor_id.partition(".")
    rule = RULES[group]
    return int(rule if not isinstance(rule, dict) else rule[key])


# ---------------------------------------------------------------------------
# collect —— 一次读全库事实（评分与攻击面共用的那一次）
# ---------------------------------------------------------------------------


def _ids_of_kind(conn: sqlite3.Connection, project_id: int,
                 kind: str) -> list[int]:
    """某 kind 的当前实体 id，升序。**过滤条件与 `query.entities_page` 一致**：
    `is_current=1 AND merged_into IS NULL` —— 合并走掉的实体不该再被数一遍。"""
    return [int(r["id"]) for r in conn.execute(
        "SELECT e.id FROM entities e WHERE e.project_id=? AND e.is_current=1"
        " AND e.merged_into IS NULL AND e.kind=? ORDER BY e.id",
        (int(project_id), str(kind)))]


def _entities_by_ids(conn: sqlite3.Connection, project_id: int,
                     ids: Iterable[int]) -> dict[int, dict[str, str]]:
    """实体 id → `{kind, key}`（分批；key 取规范 key 里字典序最小的那条 —— 与
    `query.entities_page` 的取法逐字一致，两处对"这个实体叫什么"不会有答案）。"""
    wanted = sorted({int(i) for i in ids})
    out: dict[int, dict[str, str]] = {}
    for start in range(0, len(wanted), _CHUNK):
        chunk = wanted[start:start + _CHUNK]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(
                "SELECT e.id, e.kind FROM entities e WHERE e.project_id=?"
                f" AND e.id IN ({marks})", (int(project_id), *chunk)):
            out[int(row["id"])] = {"kind": str(row["kind"]), "key": ""}
        keys: dict[int, str] = {}
        for row in conn.execute(
                "SELECT entity_id, entity_key FROM entity_keys"
                " INDEXED BY ix_entity_keys_entity"
                f" WHERE entity_id IN ({marks}) AND project_id=? AND canonical=1"
                " ORDER BY entity_key",
                (*chunk, int(project_id))):
            eid = int(row["entity_id"])
            if eid not in keys:                 # 每实体取最小规范 key（见上）
                keys[eid] = str(row["entity_key"])
        for eid, key in keys.items():
            if eid in out:
                out[eid]["key"] = key
    return out


def _canonical_keys(conn: sqlite3.Connection, project_id: int,
                    ids: list[int]) -> dict[int, str]:
    return {eid: info["key"]
            for eid, info in _entities_by_ids(conn, project_id, ids).items()}


def _vuln_observations(conn: sqlite3.Connection,
                       project_id: int) -> dict[int, dict[str, Any]]:
    """每个 vuln 实体的 `severity` / `state` 观测（当前的）。

    **多值冲突取最小 obs id 的那条**（`ORDER BY … o.id` 后首条即所取）：冲突
    本身由 Inbox 的 `value_conflict` 另行计入，评分在这里只需要一个确定的桶 ——
    "两个值各算一次"会把一条漏洞数成两条，而"取哪个"若不写死就成了非确定性。
    缺观测不是异常：severity 缺 → 按 `unknown` 档计（保守）；state 缺 → 按未处置
    计（`parsers/nuclei.DEFAULT_STATE` 的语义：没被 triage 过就是 open）。
    """
    out: dict[int, dict[str, Any]] = {}
    for row in conn.execute(
            "SELECT o.entity_id, o.id, o.semantic_field, o.value"
            " FROM observations o JOIN entities e"
            "   ON e.id=o.entity_id AND e.project_id=o.project_id"
            " WHERE o.project_id=? AND o.is_current=1 AND e.kind='vuln'"
            " AND e.is_current=1 AND e.merged_into IS NULL"
            " AND o.semantic_field IN ('severity','state')"
            " ORDER BY o.entity_id, o.semantic_field, o.id",
            (int(project_id),)):
        slot = out.setdefault(int(row["entity_id"]),
                              {"severity": None, "severity_obs_id": None,
                               "state": None, "state_obs_id": None})
        field = str(row["semantic_field"])
        if slot[f"{field}_obs_id"] is None:      # 首条（最小 id）赢，见上
            slot[field] = str(row["value"])
            slot[f"{field}_obs_id"] = int(row["id"])
    return out


def _inbox_open(conn: sqlite3.Connection, project_id: int) -> list[dict[str, Any]]:
    """全部未决 Inbox 项（id / kind / severity），按"该先看见谁"排。

    **不截断**：每条未决项都要计分，截断计数就成了谎言。`evidence` 的截断发生
    在 `score()` 里（截的是展开列表，不是计数）。排序与 `query.inbox_items` 同款
    （high 在前，同档按 id）—— 两个消费者对"前 50 条是谁"给出同一个答案。
    """
    rank = {sev: i for i, sev in enumerate(INBOX_SEVERITIES)}
    rows = [{"id": int(r["id"]), "kind": str(r["kind"]),
             "severity": str(r["severity"])}
            for r in conn.execute(
                "SELECT id, kind, severity FROM inbox_items"
                " WHERE project_id=? AND state='open' ORDER BY id",
                (int(project_id),))]
    rows.sort(key=lambda item: (rank.get(item["severity"], len(rank)), item["id"]))
    return rows


def _relations(conn: sqlite3.Connection, project_id: int) -> list[dict[str, Any]]:
    """攻击面要用的三组关系：`runs` / `served_by` / `found_on`（都取当前的）。"""
    return [{"id": int(r["id"]), "src": int(r["src_id"]), "dst": int(r["dst_id"]),
             "rel": str(r["rel"])}
            for r in conn.execute(
                "SELECT id, src_id, dst_id, rel FROM relations"
                " WHERE project_id=? AND is_current=1"
                " AND rel IN ('runs','served_by','found_on') ORDER BY id",
                (int(project_id),))]


def collect(conn: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    """把评分与攻击面要吃的事实**一次读齐**。输出里没有任何"算出来的结论" ——
    结论（分数 / 聚合）在 `score()` 与 `attack_surface()` 里，两者都是纯函数，
    吃 facts 不碰库。这层分离就是"共用因子数据、不造第二套统计"的实现方式。
    """
    pid = int(project_id)

    # -- 漏洞：实体（全集，含零观测的）+ severity/state 观测 --------------------
    vuln_ids = _ids_of_kind(conn, pid, "vuln")
    vuln_keys = _canonical_keys(conn, pid, vuln_ids)
    obs = _vuln_observations(conn, pid)
    vulns = []
    for eid in vuln_ids:
        slot = obs.get(eid, {"severity": None, "severity_obs_id": None,
                             "state": None, "state_obs_id": None})
        vulns.append({"entity_id": eid, "key": vuln_keys.get(eid, ""),
                      "severity": slot["severity"],
                      "severity_obs_id": slot["severity_obs_id"],
                      "state": slot["state"], "state_obs_id": slot["state_obs_id"]})

    # -- 其余 kind 的实体（同样是全集 + 规范 key）-----------------------------
    def of_kind(kind: str) -> list[dict[str, Any]]:
        ids = _ids_of_kind(conn, pid, kind)
        keys = _canonical_keys(conn, pid, ids)
        return [{"entity_id": eid, "key": keys.get(eid, "")} for eid in ids]

    facts: dict[str, Any] = {
        "project_id": pid,
        "vulns": vulns,
        "inbox": _inbox_open(conn, pid),
        "untested_count": query.untested_count(conn, pid),
        "untested_evidence": query.untested_entities(conn, pid,
                                                     limit=EVIDENCE_CAP),
        "out_of_scope": query.out_of_scope_entities(conn, pid),
        "services": of_kind("service"),
        "endpoints": of_kind("endpoint"),
        "secrets": of_kind("secret"),
    }

    # -- 关系 + 被指着的实体 ---------------------------------------------------
    relations = _relations(conn, pid)
    target_ids = [r["dst"] for r in relations if r["rel"] == "found_on"]
    product_ids = [r["dst"] for r in relations if r["rel"] == "runs"]
    facts["relations"] = relations
    facts["relation_targets"] = _entities_by_ids(conn, pid, target_ids)
    # 被 `runs` 指着的产品实体（攻击面行上的"在跑什么"）。只查被指着的那些 ——
    # 产品全集进攻击面只是噪音，而这层要答的问题是"这台服务在跑什么"。
    facts["run_products"] = _entities_by_ids(conn, pid, product_ids)
    return facts


# ---------------------------------------------------------------------------
# score —— 纯函数：facts → 总分 + 逐因子分解
# ---------------------------------------------------------------------------


def _bucket_severity(value: str | None) -> str:
    """观测值 → 计分桶。词表外（或缺失）一律 `unknown` —— 保守档不许静默漏计。"""
    return value if value in RULES["vuln_open"] else "unknown"


def _is_open(state: str | None) -> bool:
    """漏洞算不算"未处置"。缺 state = 没被 triage 过 = open（nuclei 的 DEFAULT_STATE）。"""
    return state is None or state == "open"


def score(facts: dict[str, Any]) -> dict[str, Any]:
    """`sum(factors[*].contribution) == total` —— 恒等式，不是巧合。

    每个因子的形状固定为：

        {"id", "label", "weight", "count", "contribution",
         "evidence": [{"type","id","label"}…], "evidence_total"}

    `evidence` 里的 `type ∈ {entity, observation, inbox_item}`，每条都**真的存在
    于库**（测试逐条查表钉住）。零命中因子照旧输出（count=0 / contribution=0）：
    输出形状不随数据漂移，GUI 与断言都不必写"有则渲染"的分支。
    """
    buckets: dict[str, list[dict[str, Any]]] = {fid: [] for fid in factor_ids()}
    counts: dict[str, int] = {fid: 0 for fid in factor_ids()}

    # -- 漏洞因子：只数 open 的（已处置的不进分，但进 facts 的状态分布）--------
    for vuln in facts["vulns"]:
        if not _is_open(vuln["state"]):
            continue
        fid = f"vuln_open.{_bucket_severity(vuln['severity'])}"
        counts[fid] += 1
        buckets[fid].append({
            "type": "observation" if vuln["severity_obs_id"] is not None
                    else "entity",
            "id": vuln["severity_obs_id"] if vuln["severity_obs_id"] is not None
                  else vuln["entity_id"],
            "label": f"{vuln['key']} · "
                     f"{vuln['severity'] or '无 severity 观测'}"
                     f" · state={vuln['state'] or 'open(缺省)'}",
        })

    # -- Inbox 因子 -----------------------------------------------------------
    for item in facts["inbox"]:
        if item["severity"] not in RULES["inbox_open"]:
            continue                              # CHECK 之外的取值不静默计分
        fid = f"inbox_open.{item['severity']}"
        counts[fid] += 1
        buckets[fid].append({"type": "inbox_item", "id": item["id"],
                             "label": f"{item['kind']} · {item['severity']}"})

    # -- 标量因子 -------------------------------------------------------------
    counts["untested"] = int(facts["untested_count"])
    buckets["untested"] = [{"type": "entity", "id": int(row["id"]),
                            "label": f"{row.get('entity_key') or '?'} · {row['kind']}"}
                           for row in facts["untested_evidence"]]
    counts["out_of_scope"] = len(facts["out_of_scope"])
    buckets["out_of_scope"] = [{"type": "entity", "id": int(row["id"]),
                                "label": f"{row['entity_key']} · {row['kind']}"}
                               for row in facts["out_of_scope"]]
    counts["exposed_service"] = len(facts["services"])
    buckets["exposed_service"] = [
        {"type": "entity", "id": int(row["entity_id"]), "label": row["key"]}
        for row in facts["services"]]
    counts["exposed_endpoint"] = len(facts["endpoints"])
    buckets["exposed_endpoint"] = [
        {"type": "entity", "id": int(row["entity_id"]), "label": row["key"]}
        for row in facts["endpoints"]]
    counts["secret"] = len(facts["secrets"])
    buckets["secret"] = [{"type": "entity", "id": int(row["entity_id"]),
                          "label": row["key"]} for row in facts["secrets"]]

    factors = []
    for fid in factor_ids():
        weight = _weight_of(fid)
        count = counts[fid]
        evidence = buckets[fid][:EVIDENCE_CAP]
        factors.append({
            "id": fid,
            "label": factor_label(fid),
            "weight": weight,
            "count": count,
            "contribution": weight * count,
            "evidence": evidence,
            "evidence_total": count,
        })
    total = sum(f["contribution"] for f in factors)
    return {"total": total, "factors": factors}


# ---------------------------------------------------------------------------
# attack_surface —— 纯函数：同一份 facts 的另一面
# ---------------------------------------------------------------------------


def attack_surface(facts: dict[str, Any]) -> dict[str, Any]:
    """暴露面按 **host → service / endpoint** 聚合。漏洞叠加用的是**与评分同一批
    行**（`facts["vulns"]` 里 open 的那些）—— 同一个口径、同一份数据，两边不可能
    各说各话。

    目标落点：`vuln --found_on--> service` 挂到那台服务上；`--> domain/ip` 直接
    挂到主机行。一个漏洞有多个 `found_on` 时**只落第一个可映射的目标**（按关系
    id 升序）—— 评分里它也只算一次，两边的计数守同一个恒等式。没有 `found_on`
    或目标认不出的进 `totals.unmapped_open_vulns`：**如实报出去**，不硬塞进某个
    主机 —— 猜一个落点比缺一个落点更坏。
    """
    hosts: dict[str, dict[str, Any]] = {}

    def host_row(host: str) -> dict[str, Any]:
        if host not in hosts:
            hosts[host] = {"host": host, "services": [], "endpoints": [],
                           "open_vulns": {sev: 0 for sev in SEVERITIES},
                           "open_vuln_total": 0}
        return hosts[host]

    for row in facts["services"]:
        host_row(_service_host(row["key"]))["services"].append(
            {"entity_id": int(row["entity_id"]), "key": row["key"],
             "products": [], "open_vulns": {sev: 0 for sev in SEVERITIES},
             "open_vuln_total": 0})
    for row in facts["endpoints"]:
        host_row(_endpoint_host(row["key"]))["endpoints"].append(
            {"entity_id": int(row["entity_id"]), "key": row["key"],
             "service_entity_id": None})

    # 关系 → 聚合行（全部从 facts 的关系表来，**不回库**）
    service_index = {s["entity_id"]: (row, s)
                     for row in hosts.values() for s in row["services"]}
    endpoint_index = {e["entity_id"]: (row, e)
                      for row in hosts.values() for e in row["endpoints"]}
    product_keys = {int(eid): info["key"]
                    for eid, info in facts["run_products"].items()}
    for rel in facts["relations"]:
        if rel["rel"] == "runs" and rel["src"] in service_index \
                and rel["dst"] in product_keys:
            service_index[rel["src"]][1]["products"].append(product_keys[rel["dst"]])
        elif rel["rel"] == "served_by" and rel["src"] in endpoint_index \
                and rel["dst"] in service_index:
            endpoint_index[rel["src"]][1]["service_entity_id"] = rel["dst"]

    found_by_src: dict[int, list[int]] = {}
    for rel in facts["relations"]:
        if rel["rel"] == "found_on":
            found_by_src.setdefault(rel["src"], []).append(rel["dst"])

    unmapped: list[dict[str, Any]] = []
    targets = facts["relation_targets"]
    for vuln in facts["vulns"]:
        if not _is_open(vuln["state"]):
            continue
        sev = _bucket_severity(vuln["severity"])
        host = None
        service_row = None
        for tid in found_by_src.get(int(vuln["entity_id"]), []):
            target = targets.get(int(tid))
            if target is None:
                continue
            if target["kind"] == "service" and int(tid) in service_index:
                host, service_row = service_index[int(tid)]
                break
            if target["kind"] in ("domain", "ip") and target["key"]:
                host = host_row(str(target["key"]))
                break
        if host is None:
            unmapped.append({"entity_id": vuln["entity_id"],
                             "key": vuln["key"], "severity": sev})
            continue
        host["open_vulns"][sev] += 1
        host["open_vuln_total"] += 1
        if service_row is not None:
            service_row["open_vulns"][sev] += 1
            service_row["open_vuln_total"] += 1

    ordered = [hosts[h] for h in sorted(hosts)]
    for row in ordered:
        row["services"].sort(key=lambda s: s["entity_id"])
        row["endpoints"].sort(key=lambda e: e["entity_id"])
        for svc in row["services"]:
            svc["products"].sort()
        row["service_count"] = len(row["services"])
        row["endpoint_count"] = len(row["endpoints"])

    totals = {"hosts": len(ordered),
              "services": sum(r["service_count"] for r in ordered),
              "endpoints": sum(r["endpoint_count"] for r in ordered),
              "open_vulns": {sev: sum(r["open_vulns"][sev] for r in ordered)
                             for sev in SEVERITIES},
              "open_vuln_total": sum(r["open_vuln_total"] for r in ordered),
              "unmapped_open_vulns": unmapped}
    return {"hosts": ordered, "totals": totals}


# ---------------------------------------------------------------------------
# overview —— 唯一的组装入口（CLI / API / GUI 都只调它）
# ---------------------------------------------------------------------------


def facts_summary(facts: dict[str, Any]) -> dict[str, Any]:
    """**全集**分布（含已处置的漏洞）—— 评分只吃 open 的，这层用来回答"为什么
    分数是这个数"的另一半：`vuln_by_state` 说清有多少条已经被处置掉了。"""
    by_severity = {sev: 0 for sev in SEVERITIES}
    by_state: dict[str, int] = {}
    for vuln in facts["vulns"]:
        by_severity[_bucket_severity(vuln["severity"])] += 1
        key = vuln["state"] if vuln["state"] is not None else "(缺 state)"
        by_state[key] = by_state.get(key, 0) + 1
    return {
        "vuln_total": len(facts["vulns"]),
        "vuln_by_severity": by_severity,
        "vuln_by_state": dict(sorted(by_state.items())),
        "inbox_open": len(facts["inbox"]),
        "untested": int(facts["untested_count"]),
        "out_of_scope": len(facts["out_of_scope"]),
        "services": len(facts["services"]),
        "endpoints": len(facts["endpoints"]),
        "secrets": len(facts["secrets"]),
    }


def overview(conn: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    """一次 collect，两份结论（评分 + 攻击面）+ 摘要。**三个消费者的唯一入口。**"""
    facts = collect(conn, project_id)
    return {
        "query": "risk",
        "rule_version": RULE_VERSION,
        "rules_digest": rules_digest(),
        "project_id": int(project_id),
        "score": score(facts),
        "attack_surface": attack_surface(facts),
        "facts": facts_summary(facts),
    }
