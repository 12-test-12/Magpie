"""`nmap` —— `nmap -oX` 的 XML 输出（计划书 §4.2 / §4.1 的 nmap 行）。

**中间件版本的主来源**。httpx 的 `tech[]` 与 `webserver` 是"看着像什么"，
nmap `-sV` 的 `service` / `product` / `version` / `cpe` 是"探到的什么"：它会把
`nginx` 探成 `product=nginx`、`version=1.24.0`，并给出
`cpe:/a:igor_sysoev:nginx:1.24.0`。计划书 §8.2 把"权威源"定义成
**CPE / 命中离线指纹 / 人工确认**，所以这一条是 §4.2 那条链路里最硬的一份证据。

产出
----

| 记录 | 内容 |
|---|---|
| `ip` 实体 | 每台主机的**主地址**（`<address addrtype="ipv4">`，IPv4 优先于 IPv6） |
| `cidr` 实体 | 扫描目标里的网段（`<target specification="10.0.0.0/24">`）—— 与 `ip` 是两个 kind |
| `domain` 实体 | 主机名（nmap 带 `-R` 反解时才出现，属于可选路径） |
| `service` 实体 | key = `host:port`（**复用 `httpx.service_key`**） |
| `product` 实体 + `runs` | 来自 `-sV` 的 `product@version`（**复用 `httpx.product_key`**） |
| 观测 | 主机级：`nmap_status` / `mac` / `hostname` / 主机脚本；服务级：port / protocol / state / reason / service / product / version / extrainfo / tunnel / cpe / cpe_vendor / cpe_range / 服务脚本 |

`<target>` 只产出一个 `cidr` 实体**加一条状态观测**，不与 `<host>` 连边：
nmap 的 `<host>` 里**没有**"我在哪个扫描目标里"这个字段（`status/@reason` 是
`syn-ack` / `arp-response` 这类响应类型，不是网段）。要连这条边只能拿地址去和
每个网段做包含判断 —— 那是在**猜** nmap 没有说的事，而 3.8 的 `member_of`
上推是"从子域真实推导"的同一条纪律：能推的推，不能推的不编。

与 httpx 不撞车（本步的核心交付标准）
------------------------------------

`httpx` 的 `tech[]` / `webserver` 已经产出过 `product` 实体，所以这里**不新造
第二套身份规则**：`service_key` / `product_key` 直接 import 自 `httpx.py`
（它俩是登记在 `kind_registry.identity_rule` 里的那两条规则的唯一实现），
CPE 的 vendor/product/version 拆解之后走 `normalize.product_name` /
`normalize.version_tuple`。于是同一台机器上

    httpx: tech=["nginx/1.24.0"]            → product: nginx@1.24.0
    nmap:  cpe:/a:igor_sysoev:nginx:1.24.0  → product: nginx@1.24.0

落到**同一个实体**上，`service --runs--> product` 因此收敛成"这个服务上跑着
nginx 1.24.0"，而不是两条互不相干的断言。

⚠️ 这一条不能靠"再写一遍同样的规则"来达成：两份实现迟早在别名表/大小写上
分叉（`Apache httpd` vs `apache2`），而分叉之后没有答案的是"哪个才对"。
所以 `httpx.product_key` 是**唯一实现**，`tests/test_parsers.py` 里有一条
对撞用例守着它（同一台机器两个文件导完，`product` 实体只能有一个）。

CPE 的两种形态
--------------

`-sV` 的 `<cpe>` 正文是 CPE URI，两种写法都要认（规范里 2.2 的 WFN 形态用双斜杠，
真实语料里两种都出现过）：

    cpe:/a:igor_sysoev:nginx:1.24.0      → part=a(应用) vendor=igor_sysoev product=nginx version=1.24.0
    cpe://a:igor_sysoev:nginx:1.24.0     → 同上（2.2 的双斜杠写法）
    cpe:/o:linux:linux_kernel:5.15       → part=o(操作系统)

`part` 必须保留：`a`（应用）/ `o`（操作系统）/ `h`（硬件）是**三种东西**。
`cpe:/o:linux:linux_kernel:5.15` 与 `cpe:/a:linux:linux_kernel:5.15` 不是同一个
实体，把它们混成一个 `linux_kernel@5.15` 会让"这台机器的中间件"里冒出一堆
内核与固件。所以 `part` 进 `product` 的**属性**，只用于展示与区分；真正的身份
仍然只有 `产品名[@版本]` —— 扩大身份维度会让"哪些还在用 Nginx 1.18"这个查询
失去意义（同一个 nginx 会因为 part 的写法差异裂成两个实体）。

`cpe:/o:` 也不提升成 `product` 之外的新 kind：它的真实形状（"操作系统"这个
资源类型）只有在阶段 3.5 的镜头与查询里才有落点，现在登记等于凭空设计一套
必然要返工的约束（计划书 §3.3 的教训）。它出现在 `product` 里由 `part` 属性
与 `cpe` 观测标明来源。

流式
----

`parse()` 拿到的是**行的迭代器**，而 XML 是跨行的 —— 这里用 `XMLPullParser`
逐块喂进去，而不是 `list(lines)` 之后拼成一个字符串：4 MB 的 XML 拼起来就是
一个 4 MB 的 str，再叠上整棵 ElementTree；而 nmap 的 `-oX` 正是那种"一台机器
几万行的 `<table>`/`<script>`"的输出。每处理完一台 `<host>` 立刻 `clear()`，
所以峰值内存在**一台主机**的量级，不随文件大小增长。

两处刻意不做的事
----------------

* **`<script>` 的正文只留骨架**。`-sV` 会带 `ssl-cert` / `http-title` /
  `smb-os-discovery` 之类，它们的输出动辄几 KB。观测里存 `script_id`（认识
  不等于入库）—— 脚本正文既不是本步的产出（§4.2 要的是 product），
  也会把 10 万观测的性能契约（§9.2 的 ⑤）直接吃掉。
* **关系只产出 `runs`**。nmap 的 `<host>` 里没有"域名解析到哪个 IP"这回事
  （那是 `-R` 反解出来的名字，不是解析事实），编一条 `resolves_to` 会把
  "这份文件说的事实"换成"我们猜的事实"。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, Iterable, Iterator

from .. import normalize
from .base import Parser, ParseContext, RawEntity, RawObservation, RawRelation, Record
from .httpx import product_key, service_key

__all__ = ["NmapParser", "cpe_breakdown", "parse_text", "split_ports"]

#: 本解析器**认识**的根元素属性（nmap 会随版本增删，出现新的即格式漂移信号）。
#: 只登记 `<nmaprun>` 的属性：它是每份文件恰好出现一次的摘要行，未知属性在那里
#: 是**真的**信号。往下逐层登记没有意义 —— `<host>` / `<hostscript>` 的属性
#: 随脚本与版本千变万化，把它们全列进来只会得到一份永远报漂移的清单。
KNOWN_ROOT_ATTRS: frozenset[str] = frozenset({
    "scanner", "args", "start", "startstr", "version", "xmloutputversion",
})

#: `<host>` 的 `<status>` 里出现 `state="up"` / `state="down"` / `state="unknown"`。
_HOST_STATES = frozenset({"up", "down", "unknown", "skipped"})

#: 一个 `<port>` 的协议（nmap 只输出这两个；第三个出现就是漂移）。
_PROTOCOLS = frozenset({"tcp", "udp", "sctp"})

#: 观测值的上限（与 `ParseContext.on_bad_line` 的 `raw[:400]` 同一口径）。
#: `<script>` 的正文是唯一会超的字段 —— 截断而不是丢弃：截断后它仍然是
#: "这个脚本跑过、说了什么"的证据，丢掉它就只剩一个 id。
#:
#: 公开（不带下划线）是刻意的：它是**契约的一部分**（报告里能看到的正文长度），
#: 测试要断言的就是这个数，而不是实现里恰好写着的那个数。
MAX_VALUE_CHARS = 400
_MAX_VALUE_CHARS = MAX_VALUE_CHARS

#: 解析前的**总字节**上限。`probe_file` 已经拦下了 zip / gzip / NUL 密集等
#: 二进制输入，这里防的是另一种：一份看起来像 XML、实际是几个 GB 的文本。
#: 超限时**不静默截断** —— 半份 XML 是解析不出东西的，报告要说出这件事。
_MAX_XML_BYTES = 512 * 1024 * 1024


class NmapParser(Parser):
    tool = "nmap"
    format_version = "1"
    known_fields = KNOWN_ROOT_ATTRS
    #: 计划书 §8.2 把 nmap `-sV` 的 CPE 列为**权威源**（与"命中离线指纹"和
    #: "人工确认"并列），所以它的可信度上限与人工确认同档，高于 httpx 的 0.95。
    max_confidence = 0.99
    #: 与 httpx **不互斥**：它们探的是同一台机器的不同层面（端口/版本 vs
    #: HTTP 响应），一份目录里常常两个都有，互斥组会把它们推向人工裁决。
    exclusive_group = ""

    # ------------------------------------------------------------------
    # 嗅探（§5.4）
    # ------------------------------------------------------------------

    @classmethod
    def sniff(cls, sample: str, filename: str) -> float:
        """只看**样本**：是不是一份 nmap XML。

        判据刻意是"nmap 自己的签名"，不是"它是不是 XML"：一份 Burp / Nessus /
        OpenVAS 的 XML 同样是 `<?xml …?>`，按"是 XML"给分会让它们全部涌向这个
        解析器，然后在解析时逐行报错 —— 那比"不采用、交人工裁决"糟得多。

        实现在样本里 `find` 而不是只看开头几行：nmap 的 `-oX` 开头是
        `<?xml …?>` + `<!DOCTYPE nmaprun>` + `<nmaprun …>`，但**大文件**在
        样本（64 KiB）里可能只剩前两者。反过来，`<!DOCTYPE nmaprun` 只可能在
        nmap 自己的输出里出现（`<?xml …?>` 后面紧跟的那一段），拿它当
        主判据既准又便宜。
        """
        del filename          # 文件名没有证明力，理由同 domainlist.sniff
        return _sniff_score(sample.replace("\x00", ""))

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------

    def parse(self, lines: Iterable[str], ctx: ParseContext) -> Iterator[Record]:
        """逐块喂 `XMLPullParser`；一台 `<host>` 关掉就立刻产出并释放它。"""
        parser = ET.XMLPullParser(events=("start", "end"))
        state = _StreamState()
        try:
            yield from self._stream(parser, lines, ctx, state)
            # `close()` 不是收尾礼仪：**截断的 XML 只有在这里才会报错**
            # （`<host><status` 这样的半截文档在 `feed()` 时是"暂未闭合"，
            # 不是错误）。漏掉它，"损坏 XML"会被报成一份 0 主机 0 坏行的空扫描。
            parser.close()
        except ET.ParseError as exc:
            state.parse_error = f"{exc}"
            # 空文件（0 字节）不算"坏 XML"：它连一个元素都没有，报"崩溃/损坏"
            # 是误报。空输入进 Inbox 由嗅探层负责（没有解析器会认领它）。
            if state.total_bytes:
                ctx.on_bad_line(ctx.current_line or 1, f"XML 解析失败：{exc}", "")
        self._wrap_up(ctx, state)

    def _stream(self, parser: ET.XMLPullParser, lines: Iterable[str],
                ctx: ParseContext, state: "_StreamState") -> Iterator[Record]:
        for line in lines:
            if ctx.cancelled():
                return
            if "\x00" in line:                      # 畸形/二进制输入：剥掉再解析
                state.nul_bytes += line.count("\x00")
                line = line.replace("\x00", "")
            state.total_bytes += len(line)
            if state.total_bytes > _MAX_XML_BYTES:
                ctx.on_bad_line(
                    ctx.current_line,
                    f"XML 超过 {_MAX_XML_BYTES} 字节上限，停止解析"
                    f"（已读 {state.total_bytes} 字节）—— 半份 XML 解析不出东西，"
                    f"所以宁可在这里说清楚，也不静默截断",
                    "")
                return
            try:
                # 喂**整行**而不是按换行切碎：XML 的报错位置是累积的，整行喂
                # 才能让行号落在出错的那一行上（`recover()` 不做，见文件头）。
                parser.feed(line)
            except ET.ParseError as exc:
                ctx.on_bad_line(ctx.current_line,
                                f"XML 解析失败：{exc}（第 {ctx.current_line} 行）",
                                line[:_MAX_VALUE_CHARS])
                return

            for event, elem in parser.read_events():
                tag = _local(elem.tag) if isinstance(elem.tag, str) else ""
                if event == "start":
                    if tag == "nmaprun" and not state.root_seen:
                        state.root_seen = True
                        # 把根元素**留到后面**再读它的子元素：`start` 事件到达时
                        # 根元素的子树还是空的（pull parsing 是流式的），此刻读
                        # `<target>` 只会读到零个 —— 这是最安静的一种失败（解析
                        # 成功、`<target>` 全部消失，谁也不会发现）。
                        state.root = elem
                        for attr in elem.keys():
                            if attr not in self.known_fields:
                                ctx.on_unknown_field(str(attr), ctx.current_line)
                    continue
                if tag != "host":
                    continue
                state.hosts += 1
                yield from self._host_records(elem, ctx)
                elem.clear()                        # 峰值内存 = 一台主机，不是整棵树
        # 目标（`<target>`）在最后处理：那时整个文档已经喂完，根元素的子树是
        # 完整的。它们只占几行，攒到最后读的代价可以忽略；而"边喂边读"读到的是
        # 空子树（见上面 root 的注释）。
        if state.root is not None:
            yield from self._targets(state.root, ctx)

    def _wrap_up(self, ctx: ParseContext, state: "_StreamState") -> None:
        """收尾说明。

        三条都是 `notes` 而**不是** bad_lines：它们描述的是"这份文件里没有东西"
        或"我们剥掉了什么"，不是"某些行有问题"。混进 bad_lines 会让那个质量信号
        失真（一份 0 坏行的空扫描会被报成 partial）。
        """
        if ctx.cancelled() or state.parse_error or not state.total_bytes:
            return                    # 已经报过解析错（或压根没输入）→ 不再补一条
        if not state.root_seen:
            ctx.on_bad_line(ctx.current_line or 1,
                            "XML 里没有找到 <nmaprun> 根元素 —— 这不是 nmap -oX 的"
                            "输出（或文件被截断了）", "")
        if state.nul_bytes:
            ctx.notes.append(
                f"剥离了 {state.nul_bytes} 个 NUL 字节后才解析（行号不变）——"
                f"带 NUL 的 XML 通常意味着无 BOM 的 UTF-16 或被截断的归档")
        if state.root_seen and state.hosts == 0:
            ctx.notes.append(
                "XML 解析成功但没有 <host> 元素 —— 主机可能全部宕机、"
                "或被 --host-timeout 跳过（这不是格式漂移）")

    # ------------------------------------------------------------------
    # `<nmaprun>` 的 `<target>` —— 这次扫描扫的是哪些网段
    # ------------------------------------------------------------------

    def _targets(self, root: ET.Element, ctx: ParseContext) -> Iterator[Record]:
        """`<target specification="10.0.0.0/24" status="skipped"/>` → `cidr` + 关系。

        **只把真正的网段提成 `cidr`**：nmap 也把单个 IP 记成 target，而那些
        会有自己的 `<host>` 与 `ip` 实体 —— 同一个地址既当 `ip` 又当 `/32`
        网段，就是 `ip` 的登记说明里点名要防的那种折叠（"把 1.2.3.4 与
        1.2.3.0/24 认成同一个实体，会让「哪台机器」的答案变成「哪一段」"）。
        反过来，单标签主机名之类无法解析的 target **只记未知字段不报坏行**：
        它很可能只是 nmap 的回显，不是这份文件要说的资产。
        """
        for target in _children(root, "target"):
            raw = str(target.get("specification") or "").strip()
            if not raw:
                continue
            cidr = normalize.try_cidr(raw)
            if cidr is None:
                if normalize.try_ip(raw) is None:
                    ctx.on_unknown_field(f"target.specification={raw}",
                                         ctx.current_line)
                continue
            yield RawEntity(kind="cidr", key=cidr, display=cidr)
            status = str(target.get("status") or "")
            if status:
                yield RawObservation(entity_ref=("cidr", cidr),
                                     type="nmap_target", value=status)

    # ------------------------------------------------------------------
    # 一台主机 → 记录
    # ------------------------------------------------------------------

    def _host_records(self, host: ET.Element, ctx: ParseContext) -> Iterator[Record]:
        address = _primary_address(host)
        if address is None:
            ctx.on_bad_line(ctx.current_line,
                            "<host> 里没有可识别的 <address>，跳过这台主机"
                            "（没有地址就没有可供挂载的服务）", "")
            return
        kind, host_key = address

        yield RawEntity(kind=kind, key=host_key, display=host_key)
        status = _child(host, "status")
        state = "" if status is None else str(status.get("state") or "")
        if state and state not in _HOST_STATES:
            ctx.on_unknown_field(f"status.state={state}", ctx.current_line)
        if state and state != "up":
            # 只记**非 up** 的状态：给每台在线主机写一条 state=up 是零信息的
            # 噪声（它会让观测数虚高，服务镜头里也会多一行没内容的门牌）——
            # 与 httpx 里 `failed` 只在真失败时才记是同一条口径。
            yield RawObservation(entity_ref=(kind, host_key), type="nmap_status",
                                 value=state)

        # MAC 与主机名挂在 host 上（IP），不挂 service：它们描述的是**这台机器**，
        # 端口有没有开都不改变它们（计划书 §4.1 的分工原则）。
        for addr in host.findall("address"):
            mac = str(addr.get("addr") or "") if addr.get("addrtype") == "mac" else ""
            if mac:
                yield RawObservation(entity_ref=(kind, host_key), type="mac", value=mac)

        hostname = ""
        for names in host.findall("hostnames"):
            for name in names.findall("hostname"):
                candidate = normalize.try_domain(name.get("name"))
                if candidate:
                    hostname = candidate
                    yield RawEntity(kind="domain", key=candidate, display=candidate)
                    yield RawObservation(entity_ref=(kind, host_key), type="hostname",
                                         value=candidate)
                    break
            if hostname:
                break

        ports = host.find("ports")
        if ports is None:
            return

        # 有地址的每一个 family 都要挂服务吗？—— 不。nmap 的 `<ports>` 是**这台
        # 主机**的，而一台机器有几个地址时端口清单是同一份。给每个地址各挂一遍
        # 会让观测数按地址数翻倍（一台双栈机器 ×2），而 §9.2 的 ⑤ 就是观测数。
        # 服务挂**主地址**（`_primary_address` 的选择是确定性的），其余地址
        # 只作为 ip 实体存在 —— 它们仍然是"这台机器有这些地址"的事实。
        for port in ports.findall("port"):
            yield from self._port_records(port, host_key, ctx)

        # 主机级脚本（`<hostscript>`）没有端口可挂，落在主机实体上。
        # **只取 `<hostscript>` 里的**：`<ports>` 里的脚本是服务级的，它们由
        # `_port_records` 挂到各自的 service 上（挂错实体是最难发现的错）。
        for hostscript in _children(host, "hostscript"):
            for script in _direct_scripts(hostscript):
                script_id = str(script.get("id") or "")
                if not script_id:
                    continue
                yield RawObservation(entity_ref=(kind, host_key), type="script",
                                     value=_script_value(script_id, script))

    def _port_records(self, port: ET.Element, host_key: str,
                      ctx: ParseContext) -> Iterator[Record]:
        raw_port = str(port.get("portid") or "")
        if not raw_port.isdigit():
            ctx.on_bad_line(ctx.current_line,
                            f"<port> 的 portid 不是端口号：{raw_port!r}", "")
            return
        protocol = str(port.get("protocol") or "").strip()
        if protocol not in _PROTOCOLS:
            ctx.on_unknown_field(f"port.protocol={protocol}", ctx.current_line)

        # service 的身份规则**不是**在这里定义的（`httpx.service_key` 才是）。
        # 端口一律原样保留（`080` 与 `80` 在 nmap 里都可能出现），因为身份是
        # "字符串相等"，而 `host:080` 与 `host:80` 会被认成两个服务 —— 所以
        # 这里先过一道规范化，让它落成同一个 key。
        svc = service_key(host_key, str(int(raw_port)), "")
        state = _child(port, "state")
        yield RawEntity(kind="service", key=svc, display=svc,
                        subtitle=f"{protocol}/{raw_port} on {host_key}")

        for name, value in (
            ("port", str(int(raw_port))),
            ("protocol", protocol),
            ("state", None if state is None else str(state.get("state") or "")),
            ("reason", None if state is None else str(state.get("reason") or "")),
        ):
            if not value:
                continue
            yield RawObservation(entity_ref=("service", svc), type=name, value=value)

        # `<port><script>` 是**服务级**脚本（`http-title` / `ssl-cert` …）。
        # 位置在 `<port>` 下而不是 `<service>` 下，这是 nmap 的实际输出形状。
        # 它必须在下面的 `return` **之前**产出：closed / filtered 的端口同样
        # 可能带着脚本输出（`-sV` 对被拒绝的端口也会给一行）。
        for script in _direct_scripts(port):
            script_id = str(script.get("id") or "")
            if not script_id:
                continue
            yield RawObservation(entity_ref=("service", svc), type="script",
                                 value=_script_value(script_id, script))

        # 被过滤掉的端口没有服务可言：`nmap -sV` 在 filtered 上只会给一个空
        # `<service method="table" conf="1"/>`。给它们造 product 等于把"探测
        # 失败"记成"探到了什么"。
        state_name = "" if state is None else str(state.get("state") or "")
        service = _child(port, "service")
        if service is None or (state_name and state_name != "open"):
            return
        yield from self._service_records(service, svc, ctx)

    def _service_records(self, service: ET.Element, svc: str,
                         ctx: ParseContext) -> Iterator[Record]:
        """`<service>` → 观测 + `product` 实体 + `service --runs--> product`。

        **CPE 优先**：`-sV` 同时给了 `<service product="nginx" version="1.24.0">`
        和 `<cpe>cpe:/a:igor_sysoev:nginx:1.24.0</cpe>` 时，两份说的通常是同一
        件事，但只有 CPE 带厂商与规范名。做法是两条都过同一套归一化，**名字相同
        就取 CPE 那一份**（版本更权威），名字不同就两条都产出（那说明 nmap 自己
        的字段与 CPE 不一致，交给数据层的冲突检测去说，而不是我们在这里猜）。
        """
        for name, value in (
            ("service", service.get("name")),
            ("product", service.get("product")),
            ("version", service.get("version")),
            ("extrainfo", service.get("extrainfo")),
            ("tunnel", service.get("tunnel")),
            ("service_method", service.get("method")),
            ("service_conf", service.get("conf")),
        ):
            if value in (None, ""):
                continue
            yield RawObservation(entity_ref=("service", svc), type=name,
                                 value=str(value))

        products: dict[str, dict[str, Any]] = {}

        name, version = _product_fields(service)
        if name:
            key = product_key(name, version)
            products.setdefault(name, {"key": key, "family": name, "version": version,
                                       "vendor": None, "part": None, "cpe": None,
                                       "source": "service"})

        for cpe in service.findall("cpe"):
            raw = (cpe.text or "").strip()
            if not raw:
                continue
            parts = cpe_breakdown(raw)
            if parts is None:
                ctx.on_bad_line(ctx.current_line,
                                f"不是可解析的 CPE：{raw!r}（期望 "
                                f"`cpe:/a:vendor:product:version` 这样的形状）", "")
                continue
            yield RawObservation(entity_ref=("service", svc), type="cpe", value=raw)
            if parts["vendor"]:
                yield RawObservation(entity_ref=("service", svc), type="cpe_vendor",
                                     value=parts["vendor"])
            if parts["version_range"]:
                # 版本区间（`1.2.3-1.2.5`）是**一条独立事实**：它说明 nmap 只能
                # 把版本收窄到一个区间，而不是"这个服务就是 1.2.3"。
                yield RawObservation(entity_ref=("service", svc), type="cpe_range",
                                     value=parts["version_range"])
            if not parts["product"]:
                ctx.on_bad_line(ctx.current_line,
                                f"CPE 里没有产品名，无法提升成 product：{raw!r}", "")
                continue
            maker = products.get(parts["product"])
            if maker is None:
                products[parts["product"]] = {
                    "key": parts["key"], "family": parts["product"],
                    "version": parts["version"], "vendor": parts["vendor"],
                    "part": parts["part"], "cpe": raw, "source": "cpe"}
            elif maker["cpe"] is None:
                # 同名的 service 字段先到：CPE 那一份更权威（带厂商与规范版本），
                # 把它换上去，**不**额外产出一个实体 —— 这正是"两处各说一次"
                # 最容易变成两个 product 实体的地方。
                maker.update({"version": parts["version"] or maker["version"],
                              "vendor": parts["vendor"], "part": parts["part"],
                              "cpe": raw, "source": "cpe"})
                maker["key"] = product_key(maker["family"], maker["version"])

        for maker in products.values():
            product = maker["key"]
            yield RawEntity(kind="product", key=product, display=maker["family"],
                            subtitle=maker["version"] or None,
                            attrs={"family": maker["family"],
                                   "version": maker["version"] or None,
                                   "vendor": maker["vendor"],
                                   "cpe_part": maker["part"],
                                   "source": maker["source"]})
            if maker["vendor"]:
                yield RawObservation(entity_ref=("product", product), type="vendor",
                                     value=maker["vendor"])
            yield RawRelation(src=("service", svc), dst=("product", product),
                              rel="runs")

        for script in _direct_scripts(service):
            script_id = str(script.get("id") or "")
            if not script_id:
                continue
            yield RawObservation(entity_ref=("service", svc), type="script",
                                 value=_script_value(script_id, script))


# ---------------------------------------------------------------------------
# 纯函数（夹具与单测直接调用它们 —— 它们就是身份规则的一部分）
# ---------------------------------------------------------------------------


def cpe_breakdown(raw: object) -> dict[str, str] | None:
    """CPE 2.2 / 2.3 URI → 五件套。**不是 CPE 就返回 `None`**（不猜、不抛）。

    拆的是 `cpe:/a:vendor:product:version:update:edition:language` 这七个位置
    （`part` 是第一个）。两处刻意的选择：

    * **`part` 只在返回值里，不进身份**。`cpe:/o:`（操作系统）与 `cpe:/a:`
      （应用）是两种东西，但它们的**实体身份**仍然是"产品名[@版本]"—— 把
      part 拉进 key 会让"哪些还在用 Nginx 1.18"问不到（同一个 nginx 会因为
      写法差异裂成两个实体）。区分留给 `attrs.cpe_part` 与展示层。
    * **版本区间原样返回，不折成区间端点**。`1.2.3-1.2.5` 折成 `1.2.3` 是
      把"nmap 只知道一个范围"谎报成"就是 1.2.3"，而后者正是"哪些还在用
      1.18"这个查询会据以行动的事实。区间留在 `version_range` 里，
      `key` 用**下界** —— 这是唯一既能让同一台机器稳定收敛、又不说谎的选择。

    版本过 `normalize.version_tuple` 校验：`version_tuple("1.24.0-beta")` 能过，
    而 `version_tuple("2.0 (C89)")` 会抛 —— 后者是"版本字段里塞了一段描述"，
    它该作为**观测**留下来，不该冒充版本。校验不过时退化为不带版本的产品名，
    并在 `version_range` 之外单独把原文交回给调用方（`detail`）。
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    # 前缀：`cpe:/` 与 `cpe://`（2.2 的老写法）都要认；大小写不敏感（`CPE:/A:`）。
    # 大小写不敏感靠 `in` 的成员判定，**不是** `.lower()` —— 大小写折叠是
    # `normalize.py` 的活（PARSE-NORM），这里只是"两个候选前缀里有没有它"。
    if text[:5] in ("cpe:/", "CPE:/"):              # 只可能是 `cpe:/` 或 `cpe://`
        body = text[5:]
        if body.startswith("//"):
            body = body[2:]
        elif body.startswith("/"):
            body = body[1:]
    else:
        return None
    fields = body.split(":")
    while len(fields) < 7:
        fields.append("")
    part, vendor, product, version, update, edition, language = fields[:7]

    # 转义：CPE 用反斜杠转义 `:` 与 `!`（`cpe:/a:foo:bar\:baz`）。这里只还原
    # 反斜杠本身，不还原 `\!` —— 后者在真实语料里几乎不出现，而多做一步就需要
    # 一个转义表（`re` 在解析器里是被禁的，见 PARSE-NORM）。
    def unescape(value: str) -> str:
        return value.replace("\\", "").strip()

    part = unescape(part)
    vendor = unescape(vendor)
    product = unescape(product)
    version = unescape(version)
    update = unescape(update)
    edition = unescape(edition)
    language = unescape(language)
    if not product:
        return None

    family = ""
    try:
        family = normalize.product_name(product)
    except normalize.NormalizeError:
        return None

    accepted, version_range, detail = _split_version(version)

    return {
        "part": part, "vendor": vendor, "product": family,
        "version": accepted, "version_range": version_range,
        "target": update, "edition": edition, "language": language,
        "key": product_key(family, accepted), "detail": detail,
    }


def _split_version(version: str) -> tuple[str, str, str]:
    """CPE 的版本字段 → `(身份用的版本, 版本区间, 无法识别的原文)`。

    三种情况必须分开，因为把它们混起来的代价各不相同：

    * **版本区间** `1.2.3-1.2.5` —— nmap 只知道一个范围。身份取下界（唯一
      既能让同一台机器稳定收敛、又不说谎的选择），区间原样交回给调用方
      存成一条观测：折成 `1.2.3` 就是把"nmap 只知道一个范围"谎报成
      "就是 1.2.3"，而后者正是「哪些还在用 1.18」会据以行动的事实。
    * **预发布** `1.24.0-rc1` —— 它**不是**区间。判据是"右半段也是版本号"：
      `rc1` 过不了 `version_tuple`，于是走预发布这条路（`version_tuple`
      本身认 `-rc1`，见 `normalize._VERSION_RE`）。
    * **不是版本** `2.0 (C89)` / `*` —— 降级成不带版本的产品名，原文单独
      交回（`detail`）：它仍然是一条该留下的观测，只是不配当身份。
    """
    if not version:
        return "", "", ""
    if "-" in version:
        lower, _, upper = version.partition("-")
        if lower and _is_version(upper):          # 右半段也是版本号 → 真的是区间
            return (lower, version, "") if _is_version(lower) else (version, "", "")
        # 右半段不是版本号 → 预发布串，整体交给 version_tuple 判。
    if _is_version(version):
        return version, "", ""
    return "", "", version


def _is_version(text: str) -> bool:
    try:
        normalize.version_tuple(text.strip())
    except normalize.NormalizeError:
        return False
    return True


def split_ports(ports: Any) -> list[str]:
    """`"80,443"` / `"22"` / `[80, 443]` → `["80", "443"]`（去重、保序）。

    nmap 的 `<extraports>` / `<ports>` 聚合行里 ports 是逗号串。这里只做拆分与
    去重：**不**做区间展开 —— `"1-1024"` 是"这一段被过滤了"，展开成 1024 个
    端口号会让一份文件产出上万条谁也不看的观测。
    """
    if ports is None or ports == "":
        return []
    raw = ports if isinstance(ports, (list, tuple)) else str(ports).split(",")
    out: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def parse_text(text: str, ctx: ParseContext | None = None) -> list[Record]:
    """便利入口（夹具与单测）：整段 XML → 记录列表。管线不走这里。"""
    if ctx is None:
        from .base import OfflineContext
        ctx = OfflineContext()
    return list(NmapParser().parse(text.splitlines(), ctx))


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


class _StreamState:
    """一次 `parse()` 的流式记账。

    刻意不是 `@dataclass`：它是**每次调用新建的可变小袋子**，字段少到构造函数
    比 dataclass 的样板还短；而且它绝不该被跨调用共享（`Parser` 是"无状态的
    单次使用者"，见 `base.Parser`）。
    """

    __slots__ = ("root_seen", "root", "total_bytes", "nul_bytes", "hosts",
                 "parse_error")

    def __init__(self) -> None:
        self.root_seen = False
        #: `<nmaprun>` 元素本身 —— `<target>` 要等文档喂完才读得到（见 `_stream`）。
        self.root = None
        self.total_bytes = 0
        self.nul_bytes = 0
        self.hosts = 0
        #: 已经报过的解析错（有它就不再补"没有 <host>"那条说明 —— 两条一起
        #: 出现时，读者会以为文件里真的有一份能解析的空扫描）。
        self.parse_error = ""


def _sniff_score(text: str) -> float:
    """`NmapParser.sniff` 的判据本体。独立成函数只为能单独自检（见测试）。"""
    if not text:
        return 0.0
    if "<!DOCTYPE nmaprun" in text or "<nmaprun " in text or "<nmaprun>" in text:
        return 0.95
    if "<!DOCTYPE nmap_output" in text:                 # nmap 4.x 的老 doctype
        return 0.9
    # 兜底：没有 doctype 的半截样本（截断的 XML 归档里很常见）。要求 `<host`
    # **与** 一个 nmap 特有的子元素同时出现 —— 只看 `<host` 的话，任何一份
    # 别家的 XML（Nessus / Burp 里也有 host 概念）都会被认领。
    if "<host " in text or "<host>" in text:
        if "<ports>" in text or "<address " in text or "<hostscript>" in text:
            return 0.9
    return 0.0


def _local(tag: Any) -> str:
    """去掉命名空间前缀（`{http://…}host` → `host`）。

    nmap 自己的输出不带命名空间，但 XML 里出现 `xmlns` 是常态（有人把 `-oX`
    的结果塞进别的文档）。不去前缀的话，整份文件会"解析成功、产出零条记录"——
    这是最安静的一种失败。
    """
    text = str(tag)
    if text.startswith("{"):
        _, _, tail = text.rpartition("}")
        return tail
    return text


def _child(elem: ET.Element, tag: str) -> ET.Element | None:
    """按**本地名**找第一个子元素（绕开命名空间）。"""
    for child in elem:
        if isinstance(child.tag, str) and _local(child.tag) == tag:
            return child
    return None


def _children(elem: ET.Element, tag: str) -> Iterator[ET.Element]:
    for child in elem:
        if isinstance(child.tag, str) and _local(child.tag) == tag:
            yield child


def _direct_scripts(elem: ET.Element) -> Iterator[ET.Element]:
    """`elem` 的**直接** `<script>` 子元素。

    刻意不递归：`<service><script>`（服务级脚本）与 `<hostscript><script>`
    （主机级脚本）是两种东西，递归收集会把服务级的 `http-title` 挂到主机上
    —— 观测挂错实体是最难发现的一类错（数字都对，含义全错）。
    """
    for child in elem:
        if isinstance(child.tag, str) and _local(child.tag) == "script":
            yield child


def _script_value(script_id: str, script: ET.Element) -> str:
    """`<script>` → 观测值：`id: 输出`，超长截断。

    截断而不是丢弃：正文里常常带着协议与版本线索（`http-title` 的标题、
    `ssl-cert` 的 CN），但一条 `-sV` 的输出动辄几 KB，10 万观测的存储与
    吞吐契约（§9.2 ⑤）扛不住把它们全存进来。
    """
    text = (script.text or "").strip().replace("\n", " ")
    while "  " in text:
        text = text.replace("  ", " ")
    if not text:
        return script_id
    return f"{script_id}: {text}"[:_MAX_VALUE_CHARS]


def _primary_address(host: ET.Element) -> tuple[str, str] | None:
    """主地址 → `(kind, key)`。IPv4 优先于 IPv6（双栈主机取 v4，确定性优先）。

    `addrtype` 是 nmap 自己给的判据，比"看这个串像不像 IP"可靠：`<address
    addr="00:11:22:33:44:55" addrtype="mac">` 走 IP 规则会被判成非法。
    """
    ipv6: tuple[str, str] | None = None
    for addr in _children(host, "address"):
        addrtype = str(addr.get("addrtype") or "")
        raw = str(addr.get("addr") or "")
        if addrtype == "ipv4":
            value = normalize.try_ip(raw)
            if value is not None:
                return "ip", value
        elif addrtype == "ipv6":
            value = normalize.try_ip(raw)
            if value is not None and ipv6 is None:
                ipv6 = ("ip", value)
    return ipv6


def _product_fields(service: ET.Element) -> tuple[str, str]:
    """`<service product="nginx" version="1.24.0">` → `(产品名, 版本)`。

    产品名走 `normalize.product_name`（别名表折叠），版本走
    `normalize.version_tuple` 校验 —— 与 `httpx.split_tech` 同一口径。区别只在
    **拆分的输入**：httpx 拿到的是一整个 `"nginx/1.24.0"` 串，要自己切开；
    这里 nmap 已经把名字与版本分成了两个属性，切分那一步不存在。

    名字不在别名表里（`netbios-ssn`、`unknown`）时**原样保留**：别名表是
    "折叠写法"，不是"白名单"。过滤掉不认识的名字等于把 nmap 探到的东西丢了。
    """
    raw_name = str(service.get("product") or service.get("name") or "").strip()
    if not raw_name:
        return "", ""
    try:
        family = normalize.product_name(raw_name)
    except normalize.NormalizeError:
        return "", ""
    # 版本的三种情况（区间 / 预发布 / 不是版本）与 CPE 那一侧共用同一个判定
    # —— 两处各写一遍的话，"nmap 的 product= 属性说 1.2.3-1.2.5、CPE 说 1.2.3"
    # 这类输入会在两个函数里得到不同的 key，然后变成两个 product 实体。
    accepted, _range, _detail = _split_version(str(service.get("version") or "").strip())
    return family, accepted
