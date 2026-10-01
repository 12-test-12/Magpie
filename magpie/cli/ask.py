"""`magpie ask` —— 本工具**唯一被允许的出站点**（5.4，默认关闭）。

设计裁定四条：

1. **默认关闭，显式开启才出站。** 开关是两个环境变量：

     * ``MAGPIE_ASK_ENABLE`` —— 只认 ``1/true/yes/on``（大小写不敏感）；
       **认不出的值一律按"未开启"处理**：默认态必须赢，一个 ``ENABLE=ture``
       的拼写错误不能变成出站许可。
     * ``MAGPIE_ASK_URL`` —— http/https 端点（scheme 检查把 ``file://`` 之类
       挡在外面；没配目标同样零调用）。

   未开启时命令在**任何网络调用之前**以可读错误（``E_ASK_DISABLED``，
   退出码 1）收场，说清三件事：这是什么、为什么默认关、如何开启。测试钉的是
   **"零调用"而不是"报错了"**（socket 封死 + attempts 记账，
   `tests/test_ask.py` 的 `DEFAULT_NO_OUTBOUND` 名单）。

2. **开启时先在 stderr 打可见横幅**（照 `read.py` 的 `_FULL_WARNING` 形状：
   每行 ⚠ 开头 + 边框，一行都不能漏眼看），至少含**目标 host**、**将发送的
   内容类别**（问题文本 / 可选上下文包片段）、以及**"这是本工具唯一被允许的
   出站点"**。横幅在**发出请求之前**打 —— 超时 / 非 2xx 的请求也已经出站了，
   警告必须与出站同刻；stdout 的 JSON 信封不被横幅污染（横幅只走 stderr）。

3. **载荷先掩码。** 出站内容（问题文本、可选上下文包）一律先过
   ``core.export.redact_value`` —— 与 `export` 默认模式**同一道**
   ``detect_secrets`` 值级防线（"明文永不外传"对齐"明文永不落库"），
   不许在这里另写一份掩码。

4. **上下文包只有显式要求才进载荷**：``--entity <id>`` 才附带 5.3 的
   Markdown 上下文包，默认只发问题文本。取数与渲染复用 MCP 的
   ``_call_agent_context``（它与 ``GET /api/v1/agent/context`` 由收敛断言
   钉成一件事）—— **惰性 import**：``adapters.mcp`` 顶层 import 了
   ``magpie.cli``，模块级反向 import 会撞进循环；`__init__._run_app` 惰性
   import ``api`` 是同款理由。

R7 与本文件
-----------

`check.py` 的 R7 红线把全仓出站点收敛到**恰好一个**声明文件 —— 本文件
（``_OUTBOUND_WHITELIST``）。四向校验：声明文件之外零出站点 / 实际 ⊆ 声明 /
**声明 ⊆ 实际**（本文件必须真的有出站点，白名单不是提前挖洞）/ 子进程
curl-wget 禁令对本文件同样生效。出站实现只用标准库 ``urllib.request``。
**想把白名单扩到第二个文件 —— 停下来先问，不许自行扩。**

失败映射（退出码按 2.2 冻结的表，全部落既有档位，不新增第 6 档）::

    E_ASK_DISABLED  1  未开启（默认态）—— 零调用
    E_USAGE         1  空问题 / --timeout 非正数
    E_ASK_CONFIG    1  开了但 URL 缺失或非 http(s)
    E_ASK_TIMEOUT   2  请求超时
    E_ASK_DNS       2  域名解析失败
    E_ASK_CONNECT   2  连接失败（拒绝 / 重置 / 其他 OSError）
    E_ASK_UPSTREAM  2  非 2xx（带 http_status）
"""

from __future__ import annotations

import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .. import __version__
from ..core import export
from .envelope import EXIT_DATA, EXIT_USAGE, CliError, usage_error

#: 出站开关与目标 URL。**机制的完整说明进 `--help`（`__init__.build_parser`
#: 的 ask description）与交付评论** —— 一条默认关闭的命令，开启方式必须在
#: 最常被读到的两个地方各自完整出现一次。
ASK_ENABLE_ENV = "MAGPIE_ASK_ENABLE"
ASK_URL_ENV = "MAGPIE_ASK_URL"

#: 判定为"开启"的字面量。认不出的值按未开启 —— 默认态赢（模块文档第 1 条）。
_ENABLED_VALUES = frozenset({"1", "true", "yes", "on"})

#: 默认请求超时（秒）。
DEFAULT_TIMEOUT = 30.0

#: 回复进信封的截断上限（字符）。回复是**远端自由文本** —— 不截断的话一次
#: 意外的大响应会把 stdout 信封撑爆，而信封是给脚本读的（截断如实进
#: `data.reply_truncated`，不静默）。
REPLY_LIMIT = 16_384


def _ask_banner(host: str, url: str, *, with_context: bool) -> str:
    """开启态打到 **stderr** 的横幅（形状照 `read._FULL_WARNING`）。

    放 stderr 而不是塞进信封：信封在 stdout、是给机器读的，混一行警告进去会把
    `json.load` 弄炸；而 stderr 在终端上**就在眼前**。三条必含内容逐行独立成行
    （目标 host / 内容类别 / "唯一出站点"），一眼扫不到就是这条横幅没写对。
    """
    lines = [
        "⚠ " + "=" * 68,
        "⚠  magpie ask：即将发起出站请求 —— 这是本工具唯一被允许的出站点！",
        f"⚠  目标 host：{host}（POST {url}）",
        "⚠  将发送：问题文本（出站前已过 detect_secrets 值级掩码）",
    ]
    if with_context:
        lines.append("⚠  将发送：上下文包片段（--entity 显式附带，同样先掩码）")
    else:
        lines.append("⚠  不发送：上下文包（--entity 显式给出才附带，本次未要求）")
    lines.append(f"⚠  默认关闭；本次由 {ASK_ENABLE_ENV}=1 + {ASK_URL_ENV} 显式开启。")
    lines.append("⚠ " + "=" * 68)
    return "\n".join(lines)


def _context_pack(args, ctx) -> str:
    """5.3 的上下文包 Markdown —— **只有 `--entity` 显式给出时**才走到这里。

    取数与渲染整个复用 MCP 的 `_call_agent_context`：它与
    `GET /api/v1/agent/context` 的逐字节收敛由
    `adapters/tests/test_protocol.py` 钉住，复用即继承那条断言（ask ≡ MCP ≡
    API），而不是引入第三份渲染。惰性 import 的理由见模块文档第 4 条。
    """
    from ..adapters.mcp import _call_agent_context
    pack = _call_agent_context(ctx.db, {
        "entity_id": int(args.entity),
        "project": getattr(args, "project", None),
    })
    return str(pack["markdown"])


def _is_dns_failure(reason: object) -> bool:
    """URLError.reason → "这是不是域名解析失败"。"""
    if isinstance(reason, socket.gaierror):
        return True
    text = str(reason).lower()
    # Windows 的 getaddrinfo 失败常以 WinError 11001 的 OSError 形态出现，
    # 不保证是 gaierror 的实例 —— 文案兜一层，两条路都判成 DNS 失败。
    return "getaddrinfo" in text or "name or service not known" in text \
        or "no such host" in text


def _post(url: str, payload: dict, timeout: float) -> tuple[int, str]:
    """唯一的一次出站调用。**只在本文件、只在这里。**

    非 2xx 由 urllib 以 `HTTPError` 抛出（它也是 `URLError` 的子类 —— 分支
    顺序必须 HTTPError 在前）。所有失败都映射成 `CliError`：不给裸 traceback
    是判据 5，`from None` 顺手掐掉"出错处再抛出"的双重栈。
    """
    host = urllib.parse.urlparse(url).netloc
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "User-Agent": f"magpie-ask/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), \
                response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise CliError(
            "E_ASK_UPSTREAM", f"目标返回 HTTP {exc.code}（{url}）",
            exit_code=EXIT_DATA,
            hint="非 2xx 按数据错收（退出码 2）。先看目标服务的日志与 "
                 f"{ASK_URL_ENV} 是否指对了端点。",
            http_status=int(exc.code)) from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if _is_dns_failure(reason):
            raise CliError(
                "E_ASK_DNS", f"域名解析失败：{host} —— {reason}",
                exit_code=EXIT_DATA,
                hint=f"检查 {ASK_URL_ENV} 的主机名与本机 DNS。") from None
        if isinstance(reason, TimeoutError):
            raise CliError(
                "E_ASK_TIMEOUT", f"请求超时（{timeout:g} 秒）：{url}",
                exit_code=EXIT_DATA,
                hint=f"用 --timeout 调大（当前 {timeout:g} 秒），"
                     "或确认目标服务可达。") from None
        raise CliError(
            "E_ASK_CONNECT", f"连不上 {host} —— {reason}",
            exit_code=EXIT_DATA,
            hint="连接被拒绝 / 被重置 / 其他传输失败都在这一档（退出码 2）。"
                 "确认服务在跑、URL 端口正确。") from None
    except TimeoutError as exc:                       # 直接抛的超时（不经 URLError）
        raise CliError(
            "E_ASK_TIMEOUT", f"请求超时（{timeout:g} 秒）：{url}",
            exit_code=EXIT_DATA,
            hint=f"用 --timeout 调大（当前 {timeout:g} 秒），"
                 "或确认目标服务可达。") from None
    except UnicodeError as exc:
        # idna 编码在 getaddrinfo 里炸（超长 label / 空 label）—— 按域名解析
        # 失败收：它是"这个名字根本没法拿去问 DNS"，与 gaierror 同一类答案。
        # 没有这一层，它会以裸 traceback 出场 —— 判据 5 说的是"不许裸 traceback"。
        raise CliError(
            "E_ASK_DNS", f"域名无法解析（名字不合法）：{host} —— {exc}",
            exit_code=EXIT_DATA,
            hint=f"检查 {ASK_URL_ENV} 里的主机名（label 超长 / 空段 / 非法字符）。") from None
    except OSError as exc:                            # 拒绝 / 重置 …
        raise CliError(
            "E_ASK_CONNECT", f"连不上 {host} —— {exc}",
            exit_code=EXIT_DATA,
            hint="连接被拒绝 / 被重置 / 其他传输失败都在这一档（退出码 2）。"
                 "确认服务在跑、URL 端口正确。") from None


def cmd_ask(args, ctx) -> tuple[dict, list[str]]:
    """把问题发给配置的端点（**默认关闭** —— 见模块文档第 1 条）。

    顺序是刻意的：**校验（零网络）→ 上下文包（零网络，只读库）→ 掩码 →
    横幅 → 出站**。开启态的配置错（URL 缺失 / 非 http(s)）同样零调用 ——
    "没配好目标"从来不构成出站理由。
    """
    question = str(args.question)
    if not question.strip():
        raise usage_error(
            "问题文本不能为空",
            hint='给一句要发送的问题，如：magpie ask "这台主机开了哪些端口？"')
    timeout = float(args.timeout)
    if timeout <= 0:
        raise usage_error(f"--timeout 必须是正数（收到 {args.timeout}）",
                          hint=f"--timeout <秒>，默认 {DEFAULT_TIMEOUT:g}。")

    # -- 1 · 默认关闭：到这里为止零网络 --------------------------------------
    raw_enable = os.environ.get(ASK_ENABLE_ENV, "")
    if raw_enable.strip().lower() not in _ENABLED_VALUES:
        note = ""
        if raw_enable.strip():
            note = (f"（{ASK_ENABLE_ENV}={raw_enable!r} 不算开启 —— 只认 "
                    f"{'/'.join(sorted(_ENABLED_VALUES))}；认不出的值按未开启）")
        raise CliError(
            "E_ASK_DISABLED",
            "magpie ask 默认关闭：这是本工具唯一被允许的出站点，未显式开启时"
            f"不发起任何网络调用{note}",
            exit_code=EXIT_USAGE,
            hint="这是什么：把问题文本发给你配置的 LLM 端点。为什么默认关："
                 "零出站是红线（R7），出站必须每次显式。如何开启：环境变量 "
                 f"{ASK_ENABLE_ENV}=1 且 {ASK_URL_ENV}=<http(s)://…>"
                 " —— 完整说明见 `magpie ask --help`。")

    url = os.environ.get(ASK_URL_ENV, "").strip()
    if not url:
        raise CliError(
            "E_ASK_CONFIG",
            f"{ASK_ENABLE_ENV} 已开启但 {ASK_URL_ENV} 没配 —— 出站目标未知",
            exit_code=EXIT_USAGE,
            hint=f"export {ASK_URL_ENV}=<http(s)://…> 再重试。"
                 "目标缺失时不会发起任何调用。")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise CliError(
            "E_ASK_CONFIG", f"{ASK_URL_ENV} 必须是 http(s) URL：{url!r}",
            exit_code=EXIT_USAGE,
            hint="只收 http:// 与 https://（scheme 检查把 file:// 之类挡在"
                 "外面）；形如 http://127.0.0.1:8000/v1/chat/completions。")

    # -- 2 · 上下文包：只有显式 --entity 才取（零网络，只读库） ---------------
    context: str | None = None
    if getattr(args, "entity", None) is not None:
        context = _context_pack(args, ctx)

    # -- 3 · 掩码：与 export 默认模式同一道防线，出站内容一个字节都不裸着 ------
    payload: dict[str, Any] = {"question": export.redact_value(question)}
    if context is not None:
        payload["context"] = export.redact_value(context)

    # -- 4 · 横幅（stderr）：与出站同刻，失败的请求也已经出站了 ----------------
    host = parsed.netloc
    print(_ask_banner(host, url, with_context=context is not None),
          file=sys.stderr)

    # -- 5 · 出站：全仓唯一的一次 --------------------------------------------
    status, reply = _post(url, payload, timeout)
    reply_truncated = len(reply) > REPLY_LIMIT
    if reply_truncated:
        reply = reply[:REPLY_LIMIT]

    data = {
        "target_host": host,
        "http_status": status,
        "context_pack": context is not None,
        "masked": True,
        "reply": reply,
        "reply_truncated": reply_truncated,
    }
    human = [f"已发送 → {host}（HTTP {status}）",
             ("载荷：问题文本 + 上下文包片段（均已值级掩码）" if context is not None
              else "载荷：仅问题文本（已值级掩码；--entity 才附带上下文包）"),
             f"回复（前 10 行，信封上限 {REPLY_LIMIT} 字符）："]
    reply_lines = reply.splitlines()[:10] or ["（空）"]
    human += ["  " + line for line in reply_lines]
    return data, human
