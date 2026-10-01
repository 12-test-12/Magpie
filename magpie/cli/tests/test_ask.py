"""`magpie ask` 的三条网（5.4 任务书的判据 1、2、4）。

三组断言各答一个问题：

  * **默认关 = 零调用**（判据 1）—— socket 的连接面封死（碰一下就留下记号并
    当场炸），跑 `ask` 的默认态与"开了但没配好"的配置态，断言钉的是
    **`attempts == []`**（一次连接都没发起），而不是"报错了"。名单登记在
    `DEFAULT_NO_OUTBOUND` —— 与 `test_readonly.READ_INVOCATIONS`（只读清单，
    ask 刻意不进那份）相对：那边管"跑完不写库"，这边管"默认态不出站"。
  * **开启态端到端**（判据 2）—— 本机回环假 LLM 服务（stdlib `http.server`，
    控制探针走 stdlib `http.client`）：横幅出现在 stderr、信封在 stdout 不被
    污染、**假服务收到的载荷已掩码**（真 secret 样式字符串进问题文本，服务端
    收到的是掩码形态 —— "明文不外传"的实证）、超时 / 非 2xx / DNS 失败走
    可读错误且无裸 traceback。
  * **契约**（判据 4 的一部分）—— 退出码只落 2.2 冻结的档位（1 = 未开启 /
    配置错，2 = 传输失败），信封形状与 `--json` 消费者所依赖的一致。
    `RUNNER_COMMANDS` 与分发用例在 `test_contract.py`。

台架豁免：`tests/` 是 R7 的台架（import 级豁免照 bench 先例），但**行为检查
不豁免** —— 本文件自己也不许出现 `socket.connect(…)` 调用点（封钉用的是
`monkeypatch.setattr`，那是给属性换值，不是发起连接）。
"""

from __future__ import annotations

import http.client
import http.server
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from conftest import ENV                                              # noqa: E402
from magpie.cli import main                                           # noqa: E402
from magpie.cli.ask import ASK_ENABLE_ENV, ASK_URL_ENV                # noqa: E402
from magpie.cli.envelope import EXIT_DATA, EXIT_USAGE                 # noqa: E402
from magpie.cli.tests.test_contract import assert_err, assert_ok      # noqa: E402
from magpie.core import export                                        # noqa: E402

#: 一条**真 secret 样式**的问题文本（github_token 形状）。判据 2 要求"塞一条
#: 真 secret 样式字符串进问题文本，服务端收到的是掩码形态" —— 掩码后的期望值
#: 直接由 `export.redact_value`（与 export 默认模式同一道防线）算出，不在这里
#: 抄一份掩码规则。
SECRET_STYLE_QUESTION = "帮我看看这台主机，token 是 " \
                        "ghp_ABCDEFGHIJKLMNOPQRSTUVWX123456 换过没有"

#: 「默认不出站」断言名单（5.4 登记位）。**新命令若默认不出站，登记到这里。**
#: 每一项都必须以可读错误收场（`code`）且全程零 socket 连接 —— 见
#: `test_default_no_outbound_commands_never_open_a_socket`。
DEFAULT_NO_OUTBOUND = (
    {"label": "默认关闭（一个环境变量都没设）",
     "env": {}, "code": "E_ASK_DISABLED"},
    {"label": "开了但没配 URL —— 目标未知，仍零调用",
     "env": {ASK_ENABLE_ENV: "1"}, "code": "E_ASK_CONFIG"},
    {"label": "配了非 http(s) 的 URL（file:// 也不许）",
     "env": {ASK_ENABLE_ENV: "1", ASK_URL_ENV: "file:///etc/passwd"},
     "code": "E_ASK_CONFIG"},
    {"label": "开启值拼写错误（认不出的值按未开启，默认态赢）",
     "env": {ASK_ENABLE_ENV: "ture"}, "code": "E_ASK_DISABLED"},
)


# ---------------------------------------------------------------------------
# 判据 1：默认关零调用（socket 封死）
# ---------------------------------------------------------------------------


def _seal_outbound(monkeypatch, attempts: list) -> None:
    """把 socket 的三个连接面封死：谁碰谁当场留下记号并炸。

    与 5.3 / 5.5 的三点钉死同款手法 —— `socket.socket.connect` 是所有出站的
    必经一步（`urllib` / `http.client` 最终也走它），`create_connection` 是
    标准库客户端的入口。三处都在，拆开写也绕不过。
    """
    def _blocked(*args, **kwargs):
        attempts.append(args)
        raise AssertionError(
            f"出站了 —— 这条路径必须零调用（args={args!r}）")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    # 钉子是活的（补丁真的落上了）—— 一颗没钉进去的钉子什么都没验到
    assert socket.socket.connect is _blocked
    assert socket.create_connection is _blocked


def test_ask_default_off_is_a_readable_error_with_zero_calls(
        empty_db, monkeypatch, capsys):
    """判据 1 的主用例：默认态 → 可读错误 + **零调用**。

    断言钉的是「零调用」而不是「报错了」：`attempts` 为空说明连接面一次都没
    被碰过；若哪天真发了请求（哪怕连接失败），`_blocked` 会先炸掉整条路径。
    """
    attempts = []
    _seal_outbound(monkeypatch, attempts)

    code = main(["ask", "这台主机开了哪些端口？", "--db", str(empty_db), "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)                 # 错误信封也在 stdout
    error = assert_err(payload, "E_ASK_DISABLED")
    assert code == EXIT_USAGE == 1                     # 冻结的表：1 = 用法错

    # 「说清三件事」：这是什么、为什么默认关、如何开启
    text = error["message"] + " " + (error.get("hint") or "")
    assert "出站点" in error["message"]
    for needle in ("MAGPIE_ASK_ENABLE", "MAGPIE_ASK_URL", "--help"):
        assert needle in text, f"错误说明缺了 {needle!r}：{text}"

    assert attempts == [], f"默认态发起了出站调用：{attempts}"
    assert "唯一被允许的出站点" not in captured.err    # 横幅不属于默认态


def test_enabled_but_unconfigured_also_makes_zero_calls(
        empty_db, monkeypatch, capsys):
    """开了 `MAGPIE_ASK_ENABLE` 但目标没配好 —— 配置错同样零调用。"""
    attempts = []
    _seal_outbound(monkeypatch, attempts)
    monkeypatch.setenv(ASK_ENABLE_ENV, "1")

    code = main(["ask", "问题", "--db", str(empty_db), "--json"])
    captured = capsys.readouterr()
    assert_err(json.loads(captured.out), "E_ASK_CONFIG")
    assert code == EXIT_USAGE
    assert attempts == [], f"配置不全却出了站：{attempts}"


def test_default_no_outbound_commands_never_open_a_socket(
        empty_db, monkeypatch, capsys):
    """`DEFAULT_NO_OUTBOUND` 名单逐条跑：**每一条都零调用**，都给可读错误。

    名单是登记位（模块文档第 1 组）：把"默认不出站"的命令/配置形态钉在这里，
    而不是散落在各条用例的字里行间 —— 漏登记的形态在这条断言里没有位置。
    """
    attempts = []
    _seal_outbound(monkeypatch, attempts)

    for case in DEFAULT_NO_OUTBOUND:
        for key in (ASK_ENABLE_ENV, ASK_URL_ENV):
            monkeypatch.delenv(key, raising=False)
        for key, value in case["env"].items():
            monkeypatch.setenv(key, value)

        code = main(["ask", "问题", "--db", str(empty_db), "--json"])
        captured = capsys.readouterr()
        assert_err(json.loads(captured.out), case["code"])
        assert code == EXIT_USAGE, case["label"]
        assert "Traceback" not in captured.err, case["label"]

    assert attempts == [], f"「默认不出站」名单里有人出了站：{attempts}"


# ---------------------------------------------------------------------------
# 判据 2：开启态端到端（回环假 LLM 服务）
# ---------------------------------------------------------------------------


class _FakeLLMHandler(http.server.BaseHTTPRequestHandler):
    """假 LLM 服务。行为由 `server.mode` 切换（ok / hang / error500）。"""

    def do_GET(self):
        """控制探针用（`http.client` 真连一次证明台架活着 —— 非空转）。"""
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.server.received.append({"path": self.path,       # noqa: SLF001
                                     "body": self.rfile.read(length)})  # noqa: SLF001
        mode = self.server.mode                               # noqa: SLF001
        if mode == "hang":
            time.sleep(5)                 # 不回 —— 客户端应当在 --timeout 内超时
            return
        if mode == "error500":
            body = json.dumps({"error": "boom"}).encode("utf-8")
            self.send_response(500)
        else:
            body = json.dumps({"reply": "pong"}).encode("utf-8")
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):                             # noqa: D102
        pass                                # 静音 —— 别往 pytest 输出里刷日志


@pytest.fixture()
def fake_llm():
    """本机回环假 LLM 服务（stdlib `http.server`；探针走 stdlib `http.client`）。

    `handle_error` 静音：hang 模式下客户端超时先断开，handler 线程的
    broken-pipe 不该在测试输出里刷 traceback（那不是被测对象）。
    """
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeLLMHandler)
    server.received = []
    server.mode = "ok"
    server.handle_error = lambda *args, **kwargs: None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # 控制探针：真连一次 —— 台架活着，下面的端到端才不是在验一个死服务
    probe = http.client.HTTPConnection("127.0.0.1", server.server_address[1],
                                       timeout=5)
    probe.request("GET", "/probe")
    assert probe.getresponse().status == 200, "假 LLM 台架没起来"
    probe.close()
    yield server
    server.shutdown()
    server.server_close()


def _url(server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions"


def _ask(args, *, env_extra: dict | None = None, timeout: int = 30):
    """真子进程跑 `magpie ask`（契约测试必须走真进程 —— test_contract 同款）。

    默认把 `no_proxy` 钉到回环：本机注册表里可能配着系统代理（实测有一台装了
    本地代理 127.0.0.1:7890），代理会替客户端连出去 —— 台架断言要的是**直连
    回环**，DNS 用例要的是**客户端自己**的 getaddrinfo 失败，两条都不许被代理
    改写。DNS 用例在自己的 `env_extra` 里把 no_proxy 放大成 `*`（旁路全部代理）。
    """
    env = dict(ENV)
    env.update({"no_proxy": "127.0.0.1,localhost", "NO_PROXY": "127.0.0.1,localhost"})
    env.update(env_extra or {})
    proc = subprocess.run([sys.executable, "-m", "magpie.cli", "ask", *args],
                          cwd=str(ROOT), capture_output=True, env=env,
                          timeout=timeout)
    return (proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


def test_ask_e2e_masked_payload_banner_on_stderr_envelope_on_stdout(
        fake_llm, empty_db):
    """开启态端到端（判据 2 的主用例）：横幅 / 信封 / 载荷掩码三样分别钉住。"""
    fake_llm.mode = "ok"
    port = fake_llm.server_address[1]
    code, out, err = _ask([SECRET_STYLE_QUESTION, "--db", str(empty_db), "--json"],
                          env_extra={ASK_ENABLE_ENV: "1", ASK_URL_ENV: _url(fake_llm)})
    assert code == 0, (out, err)

    # ① 信封在 stdout，不被横幅污染
    data = assert_ok(json.loads(out))
    assert "唯一被允许的出站点" not in out

    # ② 横幅在 stderr，三条必含内容逐条都在（目标 host / 内容类别 / 唯一出站点）
    for needle in ("唯一被允许的出站点", f"127.0.0.1:{port}",
                   "问题文本", "上下文包"):
        assert needle in err, f"横幅缺 {needle!r}：\n{err}"
    assert "Traceback" not in err

    # ③ 假服务收到的载荷：**已掩码**（明文不外传的实证）+ 默认不带上下文包
    assert len(fake_llm.received) == 1, fake_llm.received
    received = json.loads(fake_llm.received[0]["body"])
    assert received["question"] == export.redact_value(SECRET_STYLE_QUESTION)
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWX123456" not in json.dumps(
        received, ensure_ascii=False), "明文 secret 出站了"
    assert "****" in received["question"], "载荷里看不到掩码形态"
    assert "context" not in received, "默认态把上下文包塞进了载荷"

    # ④ 回复进信封（stdout 面向脚本：该有的键一个不少）
    assert "pong" in data["reply"]
    assert data["target_host"] == f"127.0.0.1:{port}"
    assert data["http_status"] == 200
    assert data["masked"] is True and data["context_pack"] is False
    assert data["reply_truncated"] is False


def test_ask_context_pack_rides_only_when_entity_is_explicit(
        fake_llm, loaded_db):
    """上下文包（5.3 Markdown）只有 `--entity` 显式给出才进载荷。"""
    fake_llm.mode = "ok"
    code, out, err = _ask(["这个实体的处置建议？", "--entity", "1",
                           "--db", str(loaded_db), "--json"],
                          env_extra={ASK_ENABLE_ENV: "1", ASK_URL_ENV: _url(fake_llm)})
    assert code == 0, (out, err)
    data = assert_ok(json.loads(out))

    received = json.loads(fake_llm.received[-1]["body"])
    assert received.get("context"), "显式 --entity 却没带上下文包"
    assert received["context"].startswith("# "), "带的不是 5.3 的 Markdown 形态"
    # 上下文包也过同一道掩码防线（值级掩码对干净文本是恒等变换）
    assert received["context"] == export.redact_value(received["context"])
    assert data["context_pack"] is True
    # 横幅如实声明这一次多带了什么
    assert "将发送：上下文包片段" in err


def test_ask_timeout_is_a_readable_error_with_the_banner_shown(
        fake_llm, empty_db):
    """超时 → 可读错误（退出码 2），无裸 traceback；横幅照打（请求已出站）。"""
    fake_llm.mode = "hang"
    code, out, err = _ask(["超时测试", "--timeout", "1",
                           "--db", str(empty_db), "--json"],
                          env_extra={ASK_ENABLE_ENV: "1", ASK_URL_ENV: _url(fake_llm)},
                          timeout=30)
    assert code == EXIT_DATA == 2, (code, out, err)
    assert_err(json.loads(out), "E_ASK_TIMEOUT")
    assert "Traceback" not in err, err
    assert "唯一被允许的出站点" in err, err


def test_ask_non_2xx_is_a_readable_error_with_the_status(fake_llm, empty_db):
    """非 2xx → 可读错误（退出码 2，带 http_status），无裸 traceback。"""
    fake_llm.mode = "error500"
    code, out, err = _ask(["坏状态测试", "--db", str(empty_db), "--json"],
                          env_extra={ASK_ENABLE_ENV: "1", ASK_URL_ENV: _url(fake_llm)})
    assert code == EXIT_DATA, (code, out, err)
    error = assert_err(json.loads(out), "E_ASK_UPSTREAM")
    assert error["http_status"] == 500
    assert "Traceback" not in err, err


def test_ask_dns_failure_is_a_readable_error(empty_db):
    """DNS 失败 → 可读错误（退出码 2），无裸 traceback。

    主机名用 `exa%mple.invalid`：`%` 不是合法主机名字符，`getaddrinfo`
    **不发查询、即时** `gaierror`（实测 0.0s）—— 比"等保留域 NXDOMAIN"稳：
    后者在离线机器上会先撞 DNS 超时（实测撞过），那是另一条已被
    `test_ask_timeout_is_a_readable_error_with_the_banner_shown` 覆盖的路径。
    `no_proxy=*` 旁路本机注册表代理：域名解析失败必须发生在**客户端自己**
    身上，经代理的失败在代理后面不可观测。
    """
    code, out, err = _ask(["DNS 失败测试", "--timeout", "5",
                           "--db", str(empty_db), "--json"],
                          env_extra={ASK_ENABLE_ENV: "1",
                                     ASK_URL_ENV: "http://exa%mple.invalid"
                                                  "/v1/chat",
                                     "no_proxy": "*", "NO_PROXY": "*"},
                          timeout=30)
    assert code == EXIT_DATA, (code, out, err)
    assert_err(json.loads(out), "E_ASK_DNS")
    assert "Traceback" not in err, err
