"""`tshark` —— Wireshark 命令行导出的 `-T json` 包列表（4.1）。

⚠️  **本机没有 tshark**，所以这份解析器**没有对着真实输出跑过**
---------------------------------------------------------------------
放行说明里写死了一条：没有的工具不许声称有。这一条对代码的影响必须说清楚，
否则读的人会把"通过"读成"验过了真导出"：

* **取值按已发布的形状写**，并因为**无法实测**而接受几种书写（见 `_field`）——
  这份宽容不是设计，是**对冲**：形状猜错的方向是"整份文件读不出一个包"，
  而那正是同一个错误在被实测时会暴露的样子；
* **夹具是按形状现写的样本，不是真导出**（`fixtures/tshark/1/input.json` 的
  文件头与交付说明里都写着这句）。它证明的是"解析器与形状自洽"，不是
  "与 Wireshark 的输出一致"；
* **缺 tshark 的可读错误路径是真验过的**（`ensure_available`）——那才是今天
  能真跑的那一半。

装上 Wireshark 之后要补的第一件事：拿一份**真导出**重跑一次黄金夹具。

为什么主路径是"离线解析已经导出的 JSON 文件"
----------------------------------------------
R7（零出站）与计划书 4.1 都写死了：**tshark 由使用者自己在外面跑好**，本工具
只读它落下来的文件。所以这个模块里没有 `subprocess`、没有 `os.system`，只有
`ensure_available()` 那一处**为了让报错可读**而做的存在性检查 —— 它不执行
任何东西。外部可执行**不是** extras（R6：`core` 仍然零第三方依赖），所以它
不进 `pyproject.toml`；缺它时抛的是可读错误，不是 `FileNotFoundError` 裸奔。

逐包读，不整份读进来
--------------------
`-T json` 的输出是一个 JSON **数组**，一个包一个对象。整份 `json.loads` 在
一份真实抓包上会吃掉几百 MB 内存，而这个解析器**没有任何东西需要整份文件**
（会话是一段一段滚出来的，见 `_flows.FlowSink`）。所以这里做的是**流式**扫描：
沿字符走，按括号配对切出一个包对象的文本，`json.loads` 它，折进会话，扔掉。
峰值内存 = 一个包，不是一份文件。

这条也是它与 har / openapi / postman 三个"整份 JSON"来源的**分歧点**，值得
写下来：那三个的文档确实需要整份才能理解（一份规范里的 `$ref` 指向文档任何
一处），而包列表天生是一串彼此独立的记录。同一个形状（JSON）不该套同一个
读法。

坏输入：一个包坏了不该带走整份抓包
------------------------------------
一个畸形包（半截 JSON / 缺 IP 层）**只跳过一个包**并记一条坏行。ARP 之类的
非 IP 包**不是坏数据** —— 它们是这份模型覆盖不到的东西（flow 的身份是 IP 的
5 元组），所以它们只报**一次**，不逐包刷屏。

⚠️  它们**算坏行**（而不是 `notes`），所以一份真实抓包**几乎总是被报成
`partial`**（真实流量里总有 ARP / STP / LLDP）。这不是"这份文件坏了"，报告里
那条坏行会说明是「N 个不构成 IP 会话的包」。为什么走坏行而不是 `notes`：同一条
形状在 `_json.walk` 里就是这个口径（"数组里有一项不是对象"记一条坏行并跳过）——
"文件里有东西没被这份模型覆盖"是一个**质量信号**，把它降成一句备注会让
`partial` 这个状态与"全都读进来了"混起来，而它正是使用者用来判断"要不要去翻
那条坏行"的东西。
"""

from __future__ import annotations

import json
import shutil
from typing import Any, Iterable, Iterator

from .. import normalize
from . import _json
from ._flows import FlowSink
from ._urls import MAX_VALUE_CHARS
from .base import OfflineContext, Parser, ParseContext, Record

__all__ = ["MAX_PACKET_CHARS", "TsharkParser", "ensure_available", "parse_text"]

#: 一个包的 JSON 文本上限。**一个包**（任何真实的以太网帧 ≤ 64 KiB，正文的
#: JSON 转义后最多几百 KB）不会接近它；超过它的东西不是在描述一个包 ——
#: 多半是这份文件根本不是 `-T json` 的输出，或者中间被别的工具拼接/包装过。
#: 报一条可读的坏行并停下，而不是把一段畸形文本吃进内存。
MAX_PACKET_CHARS = 1024 * 1024

#: `-T json` 的顶层是数组，这是它与 3.4 那三个"整份 JSON"来源（顶层是对象）
#: 的**形状判据**。嗅探靠它 ＋ 那个特征键 —— 见 `sniff`。
#: `_source` 是 tshark 的 EK 风格外壳（`{"_index","_type","_source":{"layers"}}`），
#: `layers` 是真身。两个都要出现才算认得出。
_TSHARK_MARKERS = ('"_source"', '"layers"')

#: `frame.protocols` 里的**结构性**层（链路 / 网络 / 传输）。它们不落进
#: `app_protocols` —— 那样"这段会话在跑 HTTP over TLS"会被埋进一串
#: `eth:ethertype:ip:tcp` 里。判据是"这一层是不是承载应用的那一层"，
#: 而不是一个封闭的协议名单（认不出的一律当应用层，宁可多留）。
_STRUCTURAL = frozenset({
    "eth", "ethertype", "ethernet", "sll", "sll2", "null", "linux", "raw", "data",
    "vlan", "wlan", "radiotap", "ppp", "frame", "ip", "ipv4", "ipv6", "tcp", "udp",
    "sctp", "icmp", "icmpv6", "icmpv4", "ipx", "gre", "arp",
})

#: 传输层名 → 5 元组里的协议。`frame.protocols` 里有哪个就用哪个。
_TRANSPORTS = ("tcp", "udp", "sctp")

#: `ip.proto` 的数字 → 名字（`frame.protocols` 缺了传输层时的兜底）。
#: 刻意只列三个：`ip.proto=47`（GRE）之类的名字编出来比留空更糟 ——
#: 认不出的按 `ip` 记，那是一个诚实的"这是 IP 上的什么东西"。
_IP_PROTO = {"6": "tcp", "17": "udp", "132": "sctp", "1": "icmp", "58": "icmpv6"}


def ensure_available() -> str:
    """`tshark` 在哪 —— 找不到时抛**可读错误**，不是 `FileNotFoundError`。

    形状照 3.4 的 YAML-extras 那条（`base.MissingExtrasError` 的文件头）：说清
    **缺什么**、**怎么装**、以及**本步其实不需要它**（主路径是离线解析已导出
    的文件）。最后那半句不是安慰：一个上手就以为"必须先装 Wireshark"的人，
    会去做一件这一步根本不要求他做的事。

    它**只查、不跑**。R7（零出站）与"外部可执行不是硬依赖"两条都靠这一点：
    这个函数的存在是为了让报错可读，不是为了调用它。
    """
    found = shutil.which("tshark")
    if found:
        return found
    raise _MissingTshark(
        "找不到 `tshark`：本工具需要它只有在**你想自己导出抓包**的时候 —— "
        "解析这条路读的是已经落盘的 JSON 文件，不需要它。"
        "要导出的话装 Wireshark（Windows: `winget install Wireshark.Wireshark`；"
        "Debian/Ubuntu: `apt install tshark`），然后用 "
        "`tshark -r capture.pcap -T json > capture.json` 导出，"
        "再把那份 JSON 交给 `magpie import`。"
        "（本步不替你运行 tshark —— 计划书 R7 要求零出站，"
        "抓包工具由你自己在外面跑。）")


class _MissingTshark(RuntimeError):
    """缺外部可执行。与 `base.MissingExtrasError` 同一条纪律：**具名**的异常，
    调用方与报告都能靠它把"环境问题"与"格式问题"分开。

    刻意**不**继承 `MissingExtrasError`：那个说的是"少装了一个 Python extras"，
    补救动作是 `pip install`；这个说的是"少了一个外部程序"，补救动作是装
    Wireshark。两者是**不同的动作**，混成一个类型会让报告给出错误的指引
    （`MissingExtrasError` 的文件头讲的就是这件事）。
    """


class TsharkParser(Parser):
    tool = "tshark"
    format_version = "1"
    #: EK 风格外壳的四个键（`_index` / `_type` / `_score` / `_source`）。
    #: 本解析器**不调** `report_unknown` —— 逐包报未知字段会把"这份抓包里出现了
    #: 一个新的协议层"变成几千条待办；协议层是**工具**给的元数据，不是使用者写的
    #: 数据（`_flows._protocols` 同一条口径）。这个清单留着是为了满足 §5.3 的
    #: 类属性契约，并写下来"我们认识的是哪几个"。
    known_fields = frozenset({"_index", "_type", "_score", "_source"})
    #: 与 har 同一档：抓包说的是"当时发生了什么"。同一条 404 不代表接口不存在，
    #: 一段会话也不代表服务现在还在。
    max_confidence = 0.8
    #: **不参与互斥**：一份 HAR 与一份抓包讲的是同一段流量的两个视角，它们可以
    #: 同时被导入（两组证据，不是两个竞争者）。`har` 的 `api-schema` 那一组是
    #: 四个来源争**同一套接口形状**，与这里不是一回事。
    exclusive_group = ""

    # ------------------------------------------------------------------
    # 嗅探（§5.4）
    # ------------------------------------------------------------------

    @classmethod
    def sniff(cls, sample: str, filename: str) -> float:
        """靠**顶层形状**认：一个以 `[` 开头的 JSON 数组，元素里有 `_source`
        与 `layers`。

        ⚠️ 与 har / openapi / postman / ffuf 的判据**不相交**：那四个的顶层是
        `{`（`_json.top_level_keys` 在 `[` 上直接返回空），而这里是数组。
        判据不是"试着解析一下看哪个不报错" —— 那条路的代价见 `_json.py` 的文件头
        （样本必然被截断，而坏文件上它会给出随机的归属）。

        样本被截断（`ingest.SAMPLE_BYTES` = 64 KiB）**不影响**这条判据：它只看
        前缀里的三个字符特征，不做任何解析。
        """
        del filename
        text = sample.replace("\x00", "").lstrip("﻿ \t\r\n")
        if not text.startswith("["):
            return 0.0
        head = text[:4096]
        if not all(marker in head for marker in _TSHARK_MARKERS):
            return 0.0
        return 0.95

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------

    def parse(self, lines: Iterable[str], ctx: ParseContext) -> Iterator[Record]:
        sink = FlowSink()
        #: 一眼可见的两个计数：`packets` = 真的读出来几个包（用于区分"空文件"
        #: 与"读不出包"），`not_ip` = 覆盖不到的包（只报一次，见文件头）。
        state = {"packets": 0, "not_ip": 0, "content": False}
        for index, text in enumerate(_packet_texts(lines, ctx, state), start=1):
            if ctx.cancelled():
                return
            packet = _json.load_json(text, ctx, f"第 {index} 个包")
            if packet is None:
                continue
            state["packets"] += 1
            if not isinstance(packet, dict):
                ctx.on_bad_line(index, f"第 {index} 个包不是一个 JSON 对象"
                                       f"（收到 {type(packet).__name__}）", "")
                continue
            layers = _layers_of(packet)
            if layers is None:
                ctx.on_bad_line(index, f"第 {index} 个包里没有 `_source.layers` ——"
                                       f"这不是 `tshark -T json` 的一个包",
                                json.dumps(packet, ensure_ascii=False)[:200])
                continue
            fields = _packet_fields(layers)
            if fields is None:
                # 非 IP 的包（ARP / STP / 802.1X……）。**不是坏数据**，是这份
                # 模型覆盖不到的东西 —— 只报一次（见文件头）。
                state["not_ip"] += 1
                continue
            yield from sink.observe(ctx=ctx, lineno=index, **fields)
        yield from sink.finish(ctx)

        if not state["packets"]:
            # 空文件不是坏行（与 har / ffuf 同一条口径）；**有内容却读不出包**
            # 才是 —— 那种文件在报告里长得与"解析成功但没找到"一模一样。
            if state["content"]:
                ctx.on_bad_line(1, "这份 `tshark -T json` 里一个包都读不出来 ——"
                                   "取不到任何会话", "")
        elif state["not_ip"]:
            ctx.on_bad_line(1, f"这份抓包里有 {state['not_ip']} 个不构成 IP 会话的包"
                               f"（ARP 之类），它们不进 flow ——"
                               f"flow 的身份是 IP 的 5 元组", "")


# ---------------------------------------------------------------------------
# 顶层：把一个 JSON 数组切成一个个包
# ---------------------------------------------------------------------------


def _packet_texts(lines: Iterable[str], ctx: ParseContext,
                  state: dict | None = None) -> Iterator[str]:
    """流式切分：`[` … `{…}`, `{…}` … `]` → 逐个包对象的文本。

    **不物化 `lines`**（`Parser.parse` 的硬线）：它一行一行地吃，只在**一个包**
    的范围内攒字符（`parts` 最多 `MAX_PACKET_CHARS`）。超限时报一条可读的坏行
    并**停下** —— 继续扫下去只会把同一段畸形文本重复报几千遍。

    字符串里的括号不参与配对（`"a{b"` 里的 `{` 不是结构），转义按 JSON 的规矩
    处理（`\"` 不算结束引号）。这不是一个 JSON 解析器：真解析由 `json.loads`
    在每个包上负责，这里只负责**切**。

    `state` 是给调用方回读的两个事实（`content` = 见过顶层数组、`packets` 由
    调用方自己数）：**空文件不是坏行**，而"有内容却切不出包"是 —— 两者在报告
    里必须分得开（与 har / ffuf 同一条口径）。
    """
    data = {"content": False} if state is None else state
    depth = 0
    in_string = False
    escaped = False
    started = False
    parts: list[str] = []

    for line in lines:
        if ctx.cancelled():
            return
        text = line.replace("\x00", "")
        if not data["content"]:
            # 数组之前的空白 / BOM：只在第一个非空白字符前吃掉。
            stripped = text.lstrip("﻿ \t\r\n")
            if not stripped:
                continue
            if not stripped.startswith("["):
                ctx.on_bad_line(1, "顶层不是 JSON 数组 —— `tshark -T json` 的"
                                   "顶层是一个包列表（不是对象、不是 JSONL）", "")
                return
            data["content"] = True
            text = stripped[1:]
        for ch in text:
            if not started:
                if ch in " \t\r\n,":
                    continue
                if ch == "]":
                    return                      # 包列表正常结束
                if ch != "{":
                    ctx.on_bad_line(1, f"包列表里出现了一个不是对象的元素"
                                       f"（以 {ch!r} 开头）—— 这不是 -T json 的输出", "")
                    return
                started = True
                parts = [ch]
                depth = 1
                continue
            parts.append(ch)
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield "".join(parts)
                    started = False
                    parts = []
                    continue
            if len(parts) > MAX_PACKET_CHARS:
                ctx.on_bad_line(1, f"一个包的 JSON 超过 {MAX_PACKET_CHARS} 字符的"
                                   f"上限 —— 一个包不该有这么大，这份文件多半不是"
                                   f"`tshark -T json` 的输出（解析到此为止）", "")
                return
    if started:
        ctx.on_bad_line(1, "最后一个包没有闭合（文件在包中间就结束了）——"
                           "这份导出是**截断**的，它不完整", "")


# ---------------------------------------------------------------------------
# 一个包 → 一条观测
# ---------------------------------------------------------------------------


def _layers_of(packet: dict) -> dict | None:
    """`{"_source": {"layers": {...}}}` → `layers`。

    两种外壳都收：带 `_source` 的（EK 风格的默认输出）与**直接**带 `layers`
    的（某些封装/版本）。收第二种的理由与 `_field` 的宽容同源：形状无法实测，
    而猜错的方向是"整份文件读不出一个包"。
    """
    source = packet.get("_source")
    if isinstance(source, dict) and isinstance(source.get("layers"), dict):
        return source["layers"]
    if isinstance(packet.get("layers"), dict):
        return packet["layers"]
    return None


def _field(layers: dict, name: str) -> Any:
    """按**字段的点名**取值（`ip.src` / `frame.time_epoch`）。

    ⚠️  `tshark -T json` 把层里的字段名写成了**平铺**的形式（`ip_ip_src`），
    而不同版本/封装下也可能就是点名本身（`ip.src`）或者嵌在子对象里
    （`layers["ip"]["ip_src"]`）。**没有 tshark 可以对着验**，所以这里按
    "几种可能的书写都试一遍"取值 —— 这份宽容是对冲，不是设计（文件头讲了
    为什么必须这么做，以及装上 Wireshark 之后第一件事是拿真导出复核）。

    取到列表时取**第一个**元素：某些字段（同名多值）在 tshark 里是数组，
    而 5 元组的每一件都只有一个值。这与 `_json.as_list` 的口径一致。
    """
    proto, _, field = name.partition(".")
    flat = name.replace(".", "_")
    candidates = (flat, f"{proto}_{flat}", name, f"{proto}_{field}")
    for key in candidates:
        value = layers.get(key)
        if value is not None:
            return _first(value)
    group = layers.get(proto)
    if isinstance(group, dict):
        for key in (flat, f"{proto}_{field}", field):
            value = group.get(key)
            if value is not None:
                return _first(value)
    return None


def _first(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _packet_fields(layers: dict) -> dict | None:
    """`layers` → `FlowSink.observe` 的关键字。没有 IP 层时返回 `None`。

    返回的字典**恰好**是 `observe` 认的那几个键 —— 让"一个包怎么变成一条观测"
    只有一处落点，而不是散在调用点上。
    """
    src = _field(layers, "ip.src") or _field(layers, "ipv6.src")
    dst = _field(layers, "ip.dst") or _field(layers, "ipv6.dst")
    if not isinstance(src, str) or not isinstance(dst, str) or not src or not dst:
        return None

    protocols = _stack(layers)
    proto, sport, dport = _transport(layers, protocols)
    return {
        "proto": proto,
        "src_ip": src,
        "src_port": sport,
        "dst_ip": dst,
        "dst_port": dport,
        "at": _field(layers, "frame.time_epoch") or _field(layers, "frame.time"),
        "size": _field(layers, "frame.len") or _field(layers, "frame.cap_len"),
        "protocols": _app_protocols(protocols),
    }


def _app_protocols(stack: list[str]) -> list[str]:
    """`frame.protocols` 的逐层名 → **应用层**协议名（清洗过的那一份）。

    两件事一起做，因为它们本来就是同一件事的两半：丢掉结构层（链路 / 网络 /
    传输），以及把留下的名字折成规范写法（`TLS` / `tls` 是同一种协议，而它会
    落成观测值 —— 不折的话同一段会话换个 tshark 版本就落出两个值，那是一条
    凭空出现的假冲突）。

    折的大小写走 `normalize.flow_protocol_name`（PARSE-NORM：归一化只有那一个
    入口）。认不出的名字**丢掉**，不报坏行 —— 协议栈是**工具**给的元数据，
    不是使用者写的数据（`_flows._protocols` 同一条口径）。
    """
    out: list[str] = []
    for name in stack:
        if name in _STRUCTURAL:
            continue
        try:
            out.append(normalize.flow_protocol_name(name))
        except normalize.NormalizeError:
            continue
    return out


def _stack(layers: dict) -> list[str]:
    """`frame.protocols` → 逐层协议名（`eth:ethertype:ip:tcp:tls` → 五个）。"""
    raw = _field(layers, "frame.protocols")
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else str(raw).split(":")
    return [str(item).strip() for item in items if str(item).strip()]


def _transport(layers: dict, protocols: list[str]) -> tuple[str, Any, Any]:
    """(协议, 源端口, 目的端口)。

    传输层名优先从 `frame.protocols` 里取（它是 Wireshark 自己的分层结论，
    比 `ip.proto` 的数字可靠），取不到时退回 `ip.proto` 的映射，再取不到时
    按 `ip` 记 —— "这是 IP 上的什么东西"比编一个 `tcp` 出来诚实。
    """
    name = ""
    for candidate in protocols:
        if candidate in _TRANSPORTS:
            name = candidate
            break
    if not name:
        name = _IP_PROTO.get(str(_field(layers, "ip.proto") or ""), "ip")
    if name == "ip":
        return name, None, None
    return (name,
            _field(layers, f"{name}.srcport"),
            _field(layers, f"{name}.dstport"))


def parse_text(text: str, ctx: ParseContext | None = None) -> list[Record]:
    """便利入口（夹具与单测）：整段文本 → 记录列表。管线不走这里。"""
    return list(TsharkParser().parse(text.splitlines(), ctx or OfflineContext()))
