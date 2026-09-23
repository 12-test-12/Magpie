"""敏感串扫描（4.2）—— **只计数、绝不回显原文**。

这个模块只做一件事：把一份**已经读进内存的行流**过一遍规则，产出 `secret`
实体的记录。它不读文件、不碰数据库 —— 那两件事分别是 `ingest` 的
`iter_lines` 与 `_write_record` 的活，这里只负责"哪一行里有什么形状的凭据"。

三条纪律（每一条都有它自己的理由，别合并）
------------------------------------------

**① 明文止步于本模块的局部变量。**

`normalize.detect_secrets` 返回的 `value` 是明文，本模块拿它算完身份与掩码
之后**立刻丢掉**：落进 `Record` 的每一个字段都是"明文算出来的东西"，没有
一个字段是明文本身。这不是"我们记得别写进去"—— `secret` 这个 kind 的登记表
里根本没有"原文"这一栏，所以**没有地方可以写**（这正是 4.2 的设计：与其靠
自觉，不如让那条路不存在）。判据在 `test_secrets.py` 的那条专项用例里：拿
一份带假凭据的流量导进去，明文在**整张库 + 落盘报告**里一个字节都找不到。

**② 它**不是**一个解析器（`Parser` 的子类）。**

按包扫描的发现机制（`ingest.load_parsers`）会把 `parsers/` 下的每个
`class X(Parser)` 当成一个**格式**，让它去参与嗅探仲裁、拿一份黄金夹具、
在 `produced_by` 里挂号。敏感串扫描与这些都不沾边：

  * 它**不认领文件** —— 任何格式的文件里都可能躺着凭据（这正是要扫它的理由）；
  * 它**与选中的解析器正交** —— 同一份 HAR 无论被 har 还是被 curl 解析，
    扫出来的东西必须一样。挂进仲裁就意味着"换个解析器就换一套扫描结果"。

所以它是一条**独立的管线阶段**（`ingest._run_job` 里跟在解析之后那一步），
读的是**同一个文件**、跑的是**同一份规则**，与解析器是谁无关。代价写在明处：
`kind_registry.produced_by` 里 `secret` 是**空的** —— 没有哪个解析器产出它。
这不是漏填，这是"产出者是一个阶段而不是一个格式"的唯一诚实写法
（`base.KIND_REGISTRY` 里那一行的注释把这句话钉住了）。

**③ 扫描发生在**两个归档路径之外**，所以 `stored` 与 `external` 覆盖一致。**

4.1 的 `raw_files.custody` 把"原文在谁手里"分成了两档：`stored`（复制进内容
寻址槽位）与 `external`（原文在工具之外，库里只有路径 + sha256 + 大小）。
扫描**读的是源文件**，在归档**之前**就已经跑完了，所以：

  * 它一次覆盖两条路 —— 不存在"外部文件扫不到"这回事；
  * 它**不把外部文件的正文搬进库**。这正是 4.1 那条"绝不逐包入库"要守的：
    为了扫描而复制一份正文进来，等于把 `external` 这个能力值域档位绕过去了
    （`ops.evidence_capability` 会如实说"我们只有个路径"，而库里躺着正文）。
  * 反过来说，**扫描结果里没有"原文在哪"这件事的另一半**：`secret` 实体不
    持有原文，所以它不随原文的 `storage_state` 降级而失效 —— 一条 4.1 之后
    被 `purge` 的原文，它的 `secret` 计数仍然是当时算出来的那个数（那是历史
    事实）。`secret` 的 `location` 观测指向**原始文件里的行号**，而原文没了
    之后那个行号就指不到东西了 —— 这是 3 级降级链上早就存在的一条（
    `evidence.recovery_state` 的 `hash_only` 就是同一件事），这里不另起一套。

会话与敏感串的关系：**只挂证据，不挂推理**
------------------------------------------
4.1 给每段会话算了 `flow` 实体。敏感串**不挂在会话上**：一份抓包里同一个
token 可能出现在三段会话里，而"它属于哪一段会话"是一次需要人判断的归属
（同一个 `Authorization` 头在整段抓包里就是一个凭据的重复使用）。所以本模块
只记「这个文件里有这个东西、在哪一行」，把归属留给 4.3 的指纹引擎与人工。
`secret` 与 `flow` 同属 `traffic` 镜头（计划书 §3.5 的那一行写着 `flow / secret`），
但"同镜头"说的是它们回答同一类问题，不是它们之间必须有一条边。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Iterable

from . import normalize
from .parsers.base import RawEntity, RawObservation, Record

__all__ = ["MAX_LOCATION_CHARS", "ScanResult", "Scanner", "secret_key",
           "scan_lines"]


def secret_key(rule: object, value: object) -> str:
    """`规则名:sha256(规则名:值)` —— **身份规则本身**，夹具执行的就是它。

    在 `scan.py` 而不是 `normalize.py`：`normalize` 只用标准库里的模块
    （`re` / `ipaddress` / `unicodedata` / `datetime`），`hashlib` 不在那份
    白名单里（`test_normalize_does_not_touch_the_database_or_the_write_entry`
    守着那条线）。`secret_key` 的**身份规则**（取值 / 折叠 / 校验）在
    `normalize.secret_value` / `normalize.secret_rule_name` 里，这里只做哈希。

    为什么要带规则名：同**一个**值被两条规则分别认出（一个 JWT 既是 `jwt`、
    也可能落在 `authorization_header` 里）时，它们说的是两件事 —— 「这里有一段
    JWT」与「这里有一个 Bearer 凭据」。只用 hash 会把两条合并成一条，而合并掉的
    那一半（是哪条规则先看到的）恰恰是人裁决时唯一能拿到的线索。

    为什么 hash 里要把 `rule` 一起喂进去：不这么做的话，`sha256(value)` 这个
    子串会在**每一条**同值的实体键里出现，于是"两条记录是不是同一个 token"
    可以直接比子串 —— 那等于给了库外的人一把彩虹表的钥匙（拿候选值算一次
    sha256 就知道有没有）。把规则名拌进 preimage 之后，要验证一个候选值必须
    **同时**知道它命中了哪条规则，而那一半不在攻击者的手上。
    """
    name = normalize.secret_rule_name(rule)
    text = normalize.secret_value(value)
    digest = hashlib.sha256(f"{name}:{text}".encode("utf-8")).hexdigest()
    return f"{name}:{digest}"


#: 一次扫描最多保留几个 `location` 值。同一份文件里出现几十次同一个 token 是
#: 常态（一个 token 在一份 HAR 的每个请求里都有），把它们全记下来会让观测值
#: 变成一份行号清单 —— 而问题"这个凭据出现过几次"由**报告里的 `by_rule` /
#: `occurrences`** 回答（`secret` 实体没有 `occurrence_count` 列 —— 那是
#: `inbox_items` 的列，别在这儿找它；LARI-37 审查纠正过这条注释）。
#: 保留上限同时也是**确定性**的要求：截断必须是"从前往后取前 N 个"，而不是
#: "遇到谁算谁"（后者会让同一份文件重导两次得到两组不同的行号）。
MAX_LOCATION_CHARS = 24

#: 规则名之外还要记的那两件东西的字段名。写成常量是为了让"`secret` 实体上有
#: 哪些观测"在**一处**可数 —— 4.2 的判据 3（报告里只有计数与规则名）与
#: 用例里的逐字段断言都从这一份清单出发。
RULE_FIELD = "rule"
MASKED_FIELD = "masked"


@dataclass
class _Hit:
    """一处命中在扫描过程中的**累计量**（每个 `secret` 身份一行）。"""

    key: str
    rule: str
    masked: str
    count: int = 0
    #: 行号升序、去重、最多 `MAX_LOCATION_CHARS` 个。
    lines: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class ScanResult:
    """一次扫描的结果。`records` 进管线，`by_rule` 进报告。"""

    records: list[Record]
    #: 规则名 → 命中**次数**（不是实体数）。报告只许用它 —— 见判据 3。
    by_rule: dict[str, int]
    #: 扫过的行数。报告用它说清"这次扫描看过了多少东西"（0 行的文件与
    #: 10 万行的文件报同一个 `secrets=0`，那两句话的含义完全不同）。
    lines: int
    #: 命中的**实体**数（去重之后）。与 `sum(by_rule.values())` 一般不等 ——
    #: 前者数"有几个不同的凭据"，后者数"出现了几次"。报告两个都给，因为
    #: 它们回答的是两个问题（"有没有凭据"与"泄漏面有多大"）。
    secrets: int

    def as_dict(self) -> dict:
        """报告里的那一小块 —— **只有计数与规则名，没有值**（判据 3）。

        这里没有 `masked`：掩码虽然不含随机段，但它仍然是从明文派生出来的
        文本，而"报告"是这三条落点里唯一**必然落盘**的一个（`reports/*.json`）。
        让它只带可复算的数，是这条口径最省事也最结实的形态 —— 想泄露点什么
        都无从下手，因为可用的字段里一个字符串都没有。
        """
        return {"lines": int(self.lines), "secrets": int(self.secrets),
                "occurrences": sum(self.by_rule.values()),
                "by_rule": dict(sorted(self.by_rule.items()))}


class Scanner:
    """把行流扫成 `secret` 记录。实例是"一份文件一次生命周期"的。

    与 `FlowSink` / `EndpointSink` 同一条纪律（同 `parsers/_flows.py` 的文件头）：
    解析器本身无状态，累计量住在这样一个一次性的容器里。
    """

    def __init__(self, *, file_name: str = "") -> None:
        #: 写进 `location` 的文件名。与 `evidence.locator` 里的 `file` 同源
        #: （`ingest._evidence_for_line` 用的也是 `Path(source_name).name`）——
        #: 两处落同一个文件名，是为了让人能拿 `secret` 的 location 直接去
        #: 定位 evidence，而不必再猜一次"这个行号属于哪个文件"。
        self._file_name = str(file_name)
        self._hits: dict[str, _Hit] = {}
        self.lines = 0

    # ------------------------------------------------------------------
    # 输入：一行
    # ------------------------------------------------------------------

    def feed(self, lineno: int, line: str) -> None:
        """记一行。**同一行可以命中多条规则**（一个 JWT 既是 `jwt` 也可能
        落在 `authorization_header` 里）—— 它们说的是两件事，各记各的。
        """
        self.lines += 1
        for hit in normalize.detect_secrets(line):
            key = secret_key(hit["rule"], hit["value"])
            entry = self._hits.get(key)
            if entry is None:
                entry = _Hit(key=key, rule=str(hit["rule"]),
                             masked=str(hit["masked"]))
                self._hits[key] = entry
            entry.count += 1
            last = entry.lines[-1] if entry.lines else None
            if last != lineno and len(entry.lines) < MAX_LOCATION_CHARS:
                entry.lines.append(int(lineno))

    def extend(self, numbered_lines: Iterable[tuple[int, str]]) -> None:
        for lineno, line in numbered_lines:
            self.feed(lineno, line)

    # ------------------------------------------------------------------
    # 输出：记录
    # ------------------------------------------------------------------

    def records(self) -> list[Record]:
        """累计的命中 → 记录。**顺序按身份键排序，不按发现顺序** —— 一份文件
        里同一个 token 出现在第 3 行还是第 300 行，不该改变记录的次序（它同时
        是判据 2「同一份输入跑两遍逐项相同」最容易翻车的那一处）。
        """
        out: list[Record] = []
        for key in sorted(self._hits):
            hit = self._hits[key]
            out.append(RawEntity(kind="secret", key=key, display=hit.masked,
                                 subtitle=hit.masked))
            #  观测顺序固定：规则名 → 掩码 → 位置。三者都是**值**（不是形状），
            #  所以它们走 `observation_field` 而不是 `attrs`。
            out.append(RawObservation(entity_ref=("secret", key), type=RULE_FIELD,
                                      value=hit.rule))
            out.append(RawObservation(entity_ref=("secret", key), type=MASKED_FIELD,
                                      value=hit.masked))
            out.append(RawObservation(entity_ref=("secret", key), type="location",
                                      value=self._location(hit)))
        return out

    def result(self) -> ScanResult:
        by_rule: dict[str, int] = {}
        for hit in self._hits.values():
            by_rule[hit.rule] = by_rule.get(hit.rule, 0) + hit.count
        return ScanResult(records=self.records(), by_rule=by_rule,
                          lines=self.lines, secrets=len(self._hits))

    # ------------------------------------------------------------------

    def _location(self, hit: _Hit) -> str:
        """一处命中的位置：`{"file": …, "lines": [首见, …]}`（canonical JSON）。

        为什么要一个 JSON 字符串而不是一个整数行号：`secret` 的身份是**值**，
        不是位置 —— 同一个 token 在几行里出现说的是同一件事。"出现了几次"由
        报告的 `by_rule` / `occurrences` 回答，"在文件的哪几行"由这里的
        `lines` 回答 —— 各答各的，不混进身份（4.1 的 `app_protocols` 是同一个
        取舍：排序过的列表**是一个事实**）。

        **文件名先过 `redact_secrets` 再截断**（LARI-37 向量 B）：这份观测的
        `file` 字段曾经把 `path.name` 原样落库 —— 而 issue 正文点名过"外部归档
        那条路上的文件名里可能带凭据"。掩码先于截断：反过来的话一个跨在 200 字符
        边界上的凭据会被截成半截明文，那比不掩更糟。

        文件名截断到 200 字符：一份抓包的文件名可以是任意长的路径，而 4.2 的
        观测值不该被一个文件名撑爆（同 `_urls.MAX_VALUE_CHARS` 的做法）。
        """
        file_name = normalize.redact_secrets(self._file_name)
        payload = {"file": str(file_name)[:200], "lines": list(hit.lines)}
        return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))


def scan_lines(numbered_lines: Iterable[tuple[int, str]], *,
               file_name: str = "") -> ScanResult:
    """便捷入口：一份 `(行号, 内容)` 流 → `ScanResult`。

    收的是**行流**而不是路径：这样"扫一份文件"这件事不需要一个真的文件就能跑
    （单测、bench、以及 `_scratch/` 里的探针都走这条），而**读文件**那一步
    仍然只有 `ingest.iter_lines` 一份实现（编码 / BOM / NUL / 孤立 CR 的处置
    都在那里，4.2 不重写一遍）。
    """
    scanner = Scanner(file_name=file_name)
    scanner.extend(numbered_lines)
    return scanner.result()
