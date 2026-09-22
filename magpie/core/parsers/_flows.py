"""`har` / `tshark` 共用的会话摘要（4.1）。

为什么有这么一个文件
--------------------
两个来源的输入形状毫无共同之处（一份 HTTP Archive、一份 `tshark -T json` 的包
列表，见各自的文件头），但它们要产出的东西是同一个：

    flow 实体（5 元组 + 时间窗）+ 摘要观测（包数 / 字节数 / 起止时间 / 协议）
    + `involves` 关系 + 会话目的端的主机实体

把这两遍抄下去的代价与 3.3 / 3.4 那两次是同一件事（`_urls.py` / `_apis.py` 的
文件头写透了）：**两份会各自漂移的落点**。其中一份忘了把反向的包算进字节数、
或者把空闲阈值写成另一个数，流量镜头就会对两个来源给出两套答案，而不会有任何
测试发现。

**绝不逐包入库**（设计写死的一条）
----------------------------------
计划书 4.1 点名了这条。它不是优化，是**模型**：一份 10 万包的抓包按包入库是
10 万条观测，而这张工具要回答的问题（"这台主机被谁访问过""这段会话传了多少
字节"）没有一条需要单个包。所以本模块的输入是一条一条的观测，输出是**一段
会话一行**；包级数据在 `observe()` 里被折进计数，然后就没了 —— 模块里没有
任何一个容器是"每个包一个元素"的，会话表是"每个 5 元组一个元素"的。

会话怎么切分
------------
同一个 5 元组上，两条记录的时间间隔超过 `normalize.FLOW_IDLE_SECONDS` 即
**另起一段会话**。切分规则与身份规则是同一件事的两半（`normalize.flow_identity`
的文件头讲了为什么时间窗必须进身份），所以阈值从 `normalize` 取，不在这里
另写一个数。

方向：反向的包属于**同一段**会话
--------------------------------
一个 TCP 连接是双向的，请求与响应都是它的一部分。所以会话按**无序的**两端分组
—— 只按"谁发给谁"分组会把每一次请求/响应拆成两段会话，而字节数各占一半。
而身份里的 `src` / `dst` 用**本文件里第一个包**的方向（发起方那一侧）：它在一份
文件里是确定的（同一份文件重导一次算出同一个 key），也正好是"谁发起了这段会话"。

代价写在明处：一个**从会话中途开始**的抓包会把中途第一个包的方向当成发起方向。
那是诚实的 —— 我们只报我们看到的那一段（`normalize.flow_identity` 的文件头
列着同一条代价）。

`involves` 只挂**目的**那一侧
------------------------------
5 元组的两端都会落进键里的字符串，但关系只从会话指向**目的端**：一份抓包里的
源端是自己的客户端（内网地址、临时端口），把它挂进图会让"这台主机出现在哪些
会话里"被本机的临时端口淹没；而"这段会话碰了哪个资产"才是这张图要回答的。
源端没有丢 —— 它以 `client` 观测的形式留在 flow 实体上（键里的那一半同样是
它），而 4.1 的「会话里出现的新 host 自动进 Inbox」正是靠这条关系找到待办主体。

`_` 开头的模块，发现机制不会把它当成解析器（同 `_urls.py` / `_apis.py` /
`_json.py`），但 PARSE-RO / PARSE-NORM 照常管它。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import normalize
from ._urls import MAX_VALUE_CHARS
from .base import ParseContext, RawEntity, RawObservation, RawRelation, Record

__all__ = ["FlowSink", "MAX_PROTOCOLS"]

#: 一段会话最多记几个应用层协议。一份抓包的 `frame.protocols` 是
#: `eth:ethertype:ip:tcp:tls:http` 这样的**栈**，而"这段会话在跑 HTTP over TLS"
#: 才是要记住的那件事。这一条同时是值的长度上限：一个畸形包报出几百层时，
#: 不该把观测撑爆。
MAX_PROTOCOLS = 12


@dataclass
class _Session:
    """一段会话的**累计量**。它按 5 元组分组的，不是按包 —— 见文件头。"""

    key: str                       # 身份串（`normalize.flow_identity` 算出来的）
    #: 身份串里的 `src` / `dst`（第一个包的方向：源 = 发起方，目的 = 资产侧）。
    src: str
    dst: str
    first_seen: str
    last_seen: str
    start: float                   # Unix 秒，用于空闲判定
    last: float
    packets: int = 0
    byte_count: int = 0
    protocols: set[str] = field(default_factory=set)
    #: 目的端的 `(kind, key)`（`domain` / `ip`）；`None` = 那一端没有地址。
    dst_host: tuple[str, str] | None = None
    #: 源端地址的规范写法（`client` 观测）。空 = 不知道（HAR 就是这种情况）。
    src_host: str = ""


class FlowSink:
    """把"一条一条的观测"折成会话摘要。

    实例是"一份文件一次生命周期"的（与解析器本身的无状态纪律一致，同
    `EndpointSink` / `ApiSink`）：`parse()` 进来时新建一个，跑完就扔。
    """

    def __init__(self, *, count_field: str = "packet_count") -> None:
        #: 未结束的会话：`(proto, 端点 A, 端点 B)`（A/B **已排序**）→ `_Session`。
        #: 排序过是为了让反向的包落进同一段会话（文件头"方向"那一节）。
        self._open: dict[tuple[str, str, str], _Session] = {}
        #: 会话的**开始顺序**（键）。落记录时按它走 —— 顺序必须可复现，
        #: 因为它是夹具的一部分。
        self._order: list[tuple[str, str, str]] = []
        #: 计数字段的**名字**。默认 `packet_count`（抓包的一条 = 一个包），
        #: HAR 传 `request_count`（一条 = 一次 HTTP 事务）—— 把两种不同的东西
        #: 记成同一个字段名，等于在库里写下一个说不通的数（`har.py` 的文件头）。
        self._count_field = count_field

    # ------------------------------------------------------------------
    # 输入：一条观测
    # ------------------------------------------------------------------

    def observe(self, *, proto: object, src_ip: object, src_port: object,
                dst_ip: object, dst_port: object, at: object,
                size: object = 0, protocols: object = (), ctx: ParseContext,
                lineno: int = 1) -> list[Record]:
        """记一条观测（一个包 / 一次 HTTP 事务）。

        返回**因为空闲超时而被冲掉的那段会话**的记录（多数时候是空列表）——
        调用方 `yield from` 它。这里返回 list 而不是生成器：本函数要么立即
        冲出一段会话、要么什么都不产出，没有中间状态。

        坏输入（认不出的地址 / 时间）**不抛**：记一条坏行，跳过这一条。
        一份抓包里混着一个畸形包是常态，让它带走整份文件是错的。
        """
        try:
            ident = normalize.flow_identity(proto, src_ip, src_port,
                                            dst_ip, dst_port, at)
        except normalize.NormalizeError as exc:
            ctx.on_bad_line(lineno, f"这一条算不出会话身份：{exc}",
                            str(src_ip)[:MAX_VALUE_CHARS])
            return []
        moment = self._epoch(at)
        if moment is None:
            ctx.on_bad_line(lineno, f"这一条的时间不是可识别的时间戳：{at!r}",
                            str(at)[:MAX_VALUE_CHARS])
            return []

        parts = ident["parts"]
        window = str(parts["window"])
        src, dst = str(parts["src"]), str(parts["dst"])
        group = (str(parts["proto"]), *sorted((src, dst)))

        out: list[Record] = []
        session = self._open.get(group)
        if session is not None and moment - session.last > normalize.FLOW_IDLE_SECONDS:
            out.extend(self._close(group))          # 空闲超时 → 上一段结束
            session = None
        if session is None:
            session = _Session(key=str(ident["key"]), src=src, dst=dst,
                               first_seen=window, last_seen=window,
                               start=moment, last=moment,
                               dst_host=self._host_ref(parts),
                               src_host="" if src == normalize.FLOW_UNKNOWN
                               else str(parts["src_ip"]))
            self._open[group] = session
            self._order.append(group)

        session.packets += 1
        session.byte_count += self._size(size)
        if moment > session.last:                   # 乱序的包不让时间倒流
            session.last = moment
        session.last_seen = normalize.flow_window(at)
        for name in self._protocols(protocols):
            if len(session.protocols) < MAX_PROTOCOLS:
                session.protocols.add(name)
        return out

    def finish(self, ctx: ParseContext) -> list[Record]:
        """文件读完：把还开着的会话全部落成记录（按开始的顺序）。"""
        del ctx
        out: list[Record] = []
        for group in list(self._order):
            if group in self._open:
                out.extend(self._close(group))
        return out

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _close(self, group: tuple[str, str, str]) -> list[Record]:
        session = self._open.pop(group, None)
        return [] if session is None else list(self._records(session))

    def _records(self, session: _Session) -> list[Record]:
        """一段会话 → 记录。顺序是**先实体、再观测、再关系** —— 管线按这个顺序
        认端点 kind（`ingest._write_record` 的文件头），反了端点会被兜底 kind
        建出来。"""
        key = session.key
        out: list[Record] = [RawEntity(kind="flow", key=key, display=key)]
        out.append(RawObservation(entity_ref=("flow", key), type=self._count_field,
                                  value=int(session.packets)))
        out.append(RawObservation(entity_ref=("flow", key), type="byte_count",
                                  value=int(session.byte_count)))
        out.append(RawObservation(entity_ref=("flow", key), type="first_seen",
                                  value=session.first_seen))
        out.append(RawObservation(entity_ref=("flow", key), type="last_seen",
                                  value=session.last_seen))
        if session.protocols:
            # 排序过的列表**是一个事实**（同 `_apis.declared()` 的裁定）：
            # 同一组协议换个发现顺序必须落成同一个值，否则重导同一份文件会
            # 凭空开一条冲突。
            out.append(RawObservation(entity_ref=("flow", key), type="app_protocols",
                                      value=sorted(session.protocols)))
        if session.src_host:
            out.append(RawObservation(entity_ref=("flow", key), type="client",
                                      value=session.src_host))
        if session.dst_host is not None:
            kind, host = session.dst_host
            out.append(RawEntity(kind=kind, key=host, display=host))
            out.append(RawRelation(src=("flow", key), dst=(kind, host),
                                   rel="involves"))
        return out

    @staticmethod
    def _host_ref(parts: dict) -> tuple[str, str] | None:
        """目的端 → `(kind, key)`。地址认不出（`-` / 单标签主机）时返回 `None`。

        `kind` 由地址形状决定（IP → `ip`，域名 → `domain`），与 `_urls.py` 的
        `EndpointSink` 同一条裁定 —— 同一个主机在两种来源下要落成同一个实体。
        `flow_identity` 已经把地址归一化过了，这里只做归类。
        """
        host = str(parts.get("dst_ip") or "")
        if not host or host == normalize.FLOW_UNKNOWN:
            return None
        if normalize.try_ip(host) is not None:
            return ("ip", host)
        return ("domain", host) if normalize.try_domain(host) else None

    @staticmethod
    def _protocols(raw: object) -> list[str]:
        """应用层协议名 → 清洗过的一串。非法名**丢掉**，不报坏行。

        一个包里的协议栈是**工具**给的元数据，不是使用者写的数据：为一个不认识
        的层名开一条坏行，会把"这份抓包里有几个奇怪的协议"变成"这行坏了"。
        """
        if raw is None:
            return []
        items = raw if isinstance(raw, (list, tuple, set)) else [raw]
        out: list[str] = []
        for item in items:
            try:
                out.append(normalize.flow_protocol_name(item))
            except normalize.NormalizeError:
                continue
        return out

    @staticmethod
    def _size(raw: object) -> int:
        """一条记录的字节数。认不出的（`-1` / 空 / 非数字）当 0。

        HAR 的 `bodySize` 在"响应体没有回传"时是 **-1**（规范允许），而把 -1
        当成负数累加会让一段会话的字节数变成负的 —— 一个说不通的摘要比一个
        偏小的摘要糟。所以负数在这里按 0 处理。
        """
        if isinstance(raw, bool) or raw is None:
            return 0
        if isinstance(raw, (int, float)):
            return int(raw) if raw > 0 else 0
        text = str(raw).strip()
        if not text.lstrip("-").isdigit() or not text.lstrip("-").isascii():
            return 0
        value = int(text)
        return value if value > 0 else 0

    @staticmethod
    def _epoch(raw: object) -> float | None:
        """时间戳 → Unix 秒。认不出时返回 `None`（调用方报坏行）。

        与 `normalize.flow_window` 认同样两种形状，但这里要的是**用于比较**的
        数值 —— 空闲阈值判的是两条记录的间隔，拿格式化过的字符串比不出来。
        """
        if isinstance(raw, bool) or raw is None:
            return None
        if isinstance(raw, (int, float)):
            return float(raw)
        text = str(raw).strip()
        try:
            return float(text)
        except ValueError:
            pass
        try:
            stamp = normalize.flow_window(text)
        except normalize.NormalizeError:
            return None
        return _parse_stamp(stamp)


def _parse_stamp(stamp: str) -> float:
    """`YYYY-MM-DDTHH:MM:SSZ` → Unix 秒。**只在离线路径上跑**，不是热路径。"""
    from datetime import datetime, timezone
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc).timestamp()
