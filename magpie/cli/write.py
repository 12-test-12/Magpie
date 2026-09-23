"""写命令的实现：`project create` / `import` / `undo` / `redo` / `jobs`（2.6）。

垂直切片在这里闭合：**文件 → 库 → 命令行**。2.2 把"读"这条路的风险压到最低、
冻结了契约；这个模块补上另一半，而它带来的风险是质变的 —— 读错了只是答案错，
写错了是**数据错**。所以三条纪律写在这里，比写进任何一条测试里都重要：

1. **一条写库的 SQL 都没有。** 全部经 `magpie/core` 的受控写入口
   （`ops.create_project` / `ingest.ingest_paths` / `ops.undo` / `ops.redo`）。
   R1"写入口唯一"的全部价值就在这里：撤销、身份、冲突折叠这些事**只有一份
   实现**，CLI 与 API 是同一份实现的两个人机入口，不是两套。`magpie/cli/**`
   里出现一句 `INSERT INTO …` 会让 `test_cli_source_contains_no_write_sql`
   变红 —— 那条静态断言是这条纪律唯一的执行者。

2. **退出码与 2.7 的 HTTP 状态码一一对应，不是另定一套。** 冻结的表只有六档，
   于是"这次失败该落哪一档"有一个**可判定的答案**，而不是一次自由裁量：

       0 成功  ↔ 2xx  —— 包括"跑完了但报告里有 needs_confirmation / failed 之外的
                          瑕疵"（redo 的 `applied=0` + `blocked` 就是这种：
                         `/jobs/{id}/redo` 返回 200，所以 CLI 也退出 0，把那个
                         事实写进 `data` 与 `--human` 的 ⚠，而不是偷偷借用一个
                         不存在的退出码）
       1 用法错 ↔ 400 —— 参数写错（缺 `<job>`、`--limit` 为负、JSON 解析不了）
       2 数据错 ↔ 404 —— 你指的东西不存在或不可用（作业 id、路径、库文件、
                         文件坏了 / 是二进制 / 没有解析器认领且**没有**进待裁决）
       3 需人工裁决 ↔ 409 —— 这一步得有人做决定才能继续：项目重名
                         （`POST /projects` 就是 409）、文件没有解析器被采用
                         （`needs_confirmation`，已进 Inbox）
       4 锁冲突 ↔ 423 —— 另一个进程正在写（`sqlite3.OperationalError`，见 `db._map_sqlite`）
       5 版本不匹配 ↔ 426 —— schema 比程序新 / 旧

3. **所有写命令的 `--dry-run` 干跑与真跑输出同构**（只有 COMMIT→ROLLBACK 之差）。
   这不是本模块加的语义，是数据层本来就有的：`ops.undo` / `ops.redo` 在
   `dry_run=True` 时是在事务里真的 INSERT/UPDATE 之后再 ROLLBACK，报告一模一样。
   本模块做的事只有一件 —— **把它透出去**，而不是在 CLI 里另写一个"预览"。

`import` 的失败判定（哪一档、为什么）
------------------------------------
`ingest` 的报告里每个文件有六种 state，而退出码只有三档能用。映射写在
`_IMPORT_VERDICTS` 里，一处声明、一处执行，并有一条测试逐档对着看。

一个反直觉但重要的取舍：**`import` 失败时，报告仍然原样交出去**（挂在错误信封
的 `report` 上）。理由是报告是导入**唯一**的交付通道，而"3 个文件里 2 个坏了"
这件事的全部细节只能从它读出来；只回一句 `E_IMPORT_FAILED` 会让调用方为了看
细节而重跑一次 —— 而重跑一次导入是有副作用的。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..core import ingest, normalize, ops
from .db import resolve_data_dir, resolve_project
from .envelope import (
    EXIT_DATA,
    EXIT_HUMAN,
    EXIT_USAGE,
    CliError,
    table,
)

#: **本模块登记的命令名**。`__init__.py` 把它并进 `WRITABLE`，据以决定每条命令
#: 拿只读连接还是可写连接（`Database(read_only=…)`）。放在实现旁边而不是入口
#: 旁边：加了命令却忘了登记，症状是"新命令拿到一条只读连接，写的时候才炸"，
#: 而这种错误只在真跑时出现。
#:
#: 注意 `jobs` **不在**这张表里 —— 它是只读的，只是与作业历史放在一起便于对照。
WRITE_COMMANDS = ("project", "import", "undo", "redo")

#: 作业历史默认返回多少条。**与 2.7 的 `routes.DEFAULT_JOB_LIMIT` 一致** ——
#: 作业是"最近发生了什么"，一次导入就会产生 N 条（每个文件一条），翻页在这里
#: 比多给几条更烦人。`jobs` 也不是分页的（没有游标），所以这个上限必须写进输出。
DEFAULT_JOB_LIMIT = 100
MAX_JOB_LIMIT = 500

#: 与 2.7 的 `routes._JOB_STATES` 逐字一致。**不在这里另立一套枚举** ——
#: 同一列上有两套取值，迟早在某个调用方那里分叉。
JOB_STATES = ("running", "done", "failed", "partial", "aborted")

#: `jobs` 列表取的列。与 2.7 的 `routes._JOB_COLUMNS` 同义（同一份见 `read.py`
#: 顶部那条"已知技术债"：`ls` / `show` 的 SQL 与 `api/routes.py` 是同一份语义的
#: 两份实现，§3.5 的 `core/query.py` 落地后应当收敛到一处。`jobs` 也记在那笔账上）。
_JOB_COLUMNS = ("id, project_id, kind, state, applied, coverage, scope_version,"
                " scope_hash, parser_fingerprint, actor, started_at, finished_at")


# ---------------------------------------------------------------------------
# 公共零件
# ---------------------------------------------------------------------------


def _positive_int(text: str, *, what: str) -> int:
    """`<job>` 这类"必须是一个正整数"的位置参数。"""
    raw = str(text).strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    raise CliError(
        "E_BAD_JOB_ID", f"{what} 必须是一个正整数作业 id，收到 {text!r}",
        exit_code=EXIT_USAGE,
        hint="作业 id 由导入产生，`magpie jobs --json` 列出全部（`id` 那一列）。")


def _limit(text: Any, *, ceiling: int) -> int:
    try:
        value = int(text)
    except (TypeError, ValueError):
        raise CliError("E_BAD_LIMIT", f"limit 必须是整数，收到 {text!r}",
                       exit_code=EXIT_USAGE) from None
    if value < 1:
        raise CliError("E_BAD_LIMIT", f"limit 必须 >= 1，收到 {value}",
                       exit_code=EXIT_USAGE)
    return min(value, ceiling)


def _json_or_none(raw: Any) -> Any:
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return raw


def _job_row(conn: sqlite3.Connection, job_id: int) -> dict[str, Any]:
    """作业行。**不存在就是退出码 2**（↔ 404），与 `GET /jobs/{id}` 同义。"""
    row = conn.execute(f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id=?",
                       (job_id,)).fetchone()
    if row is None:
        raise CliError("E_JOB_NOT_FOUND", f"作业 {job_id} 不存在", exit_code=EXIT_DATA,
                       hint="作业 id 由导入产生，`magpie jobs --json` 列出全部；"
                            "被 undo 过的作业**仍然存在**（`applied=0`），照样能 redo。")
    return dict(row)


def _job_counts(conn: sqlite3.Connection,
                job_ids: list[int]) -> dict[int, dict[str, int]]:
    """每个 job 的贡献数 / 已撤回数。**一次查完，不做 N+1。**

    与 `routes._job_counts` 同义。`retracted` 就是 undo 的可逆性证据：undo 一次
    = 这个数从 0 变成贡献数，redo 一次 = 变回 0。界面据此判断"这个作业现在处于
    哪一侧"，而不是自己记账 —— 所以这里也不自己记账。
    """
    out = {int(j): {"contributions": 0, "retracted": 0} for j in job_ids}
    if not job_ids:
        return out
    marks = ",".join("?" * len(job_ids))
    for row in conn.execute(
            "SELECT job_id, COUNT(*) AS n FROM contributions"
            f" WHERE job_id IN ({marks}) GROUP BY job_id", tuple(job_ids)):
        out[int(row["job_id"])]["contributions"] = int(row["n"])
    for row in conn.execute(
            "SELECT superseded_by_job_id AS jid, COUNT(*) AS n"
            " FROM contribution_supersessions"
            f" WHERE superseded_by_job_id IN ({marks}) AND reason='retract'"
            " GROUP BY superseded_by_job_id", tuple(job_ids)):
        out[int(row["jid"])]["retracted"] = int(row["n"])
    return out


def _decorate_job(conn: sqlite3.Connection, item: dict[str, Any]) -> dict[str, Any]:
    """补上"现在能不能撤 / 能不能复原"。与 `routes._decorate_job` 逐字段同义。

    `can_undo` / `can_redo` 由 `applied` 一处推出，而不是各算各的：**两个布尔
    值互为否定**这件事必须由构造保证，不能由"我记得两边都改"保证。
    """
    item["applied"] = bool(item["applied"])
    item["counts"] = _job_counts(conn, [int(item["id"])])[int(item["id"])]
    item["can_undo"] = bool(item["applied"])
    item["can_redo"] = not bool(item["applied"])
    return item


# ---------------------------------------------------------------------------
# project create —— 空库的第一件事
# ---------------------------------------------------------------------------

#: `--scope` 里只允许出现的键。多一个键不是"顺手兼容"，而是把一个错别字变成
#: 一条**永远不生效**的 scope —— 存进去了、看起来也对，但它不约束任何东西。
SCOPE_KEYS = ("include", "exclude", "parser_fingerprint")


def cmd_project_create(args, ctx) -> tuple[dict, list[str]]:
    """建项目。**与 `POST /projects` 同义**（重名同样返回 409 ↔ 退出码 3）。

    为什么它在 2.6：`init` 刻意不建项目（"项目的创建是写路径（2.6 / GUI）的事"
    —— 见 `read.cmd_init` 的 docstring），而**没有项目就没有地方导入**。少了它，
    「文件 → 库 → 命令行」这条链路在空库上根本起不了步。
    """
    name = str(args.name).strip()
    if not name:
        raise CliError("E_USAGE", "项目名不能为空", exit_code=EXIT_USAGE)
    scope = _scope_from_args(args)

    conn: sqlite3.Connection = ctx.conn()
    if conn.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone():
        raise CliError(
            "E_PROJECT_EXISTS", f"项目 {name!r} 已存在", exit_code=EXIT_HUMAN,
            hint="换一个名字，或直接用已有的那个（`magpie schema --json` 的 "
                 "projects 列出全部）。**本命令不会替你改名或覆盖。**",
            name=name)

    existing = [{"id": int(r["id"]), "name": str(r["name"])}
                for r in conn.execute("SELECT id, name FROM projects ORDER BY id")]
    project_id = ops.create_project(conn, name, scope)
    row = conn.execute("SELECT id, name, scope, scope_version, scope_hash"
                       " FROM projects WHERE id=?", (project_id,)).fetchone()
    data = {"id": int(row["id"]), "name": str(row["name"]),
            "scope": _json_or_none(row["scope"]) or {},
            "scope_version": int(row["scope_version"]),
            "scope_hash": str(row["scope_hash"])}
    human = [f"建项目 #{data['id']} {data['name']}",
             f"  scope v{data['scope_version']}：include={data['scope'].get('include') or []}"
             f"  exclude={data['scope'].get('exclude') or []}",
             "  下一个项目 id 从 1 开始连续分配，中途删除不复用。" if not existing
             else f"  库里现在有 {len(existing) + 1} 个项目 —— 此后每条命令都要"
                  f"`--project {data['id']}`（或项目名）指明是哪一个。"]
    return data, human


def _scope_from_args(args) -> dict[str, Any]:
    """`--include` / `--exclude` / `--scope` → 一个 scope 字典。

    三种写法互斥（argparse 的互斥组拦不住"列表 + 字符串"这种组合，所以这里
    自己判）。语义与 `POST /projects` 的 `scope: {...}` 完全一致 —— 本函数只
    负责把命令行上的两种写法折成那个字典。
    """
    includes = list(args.include or [])
    excludes = list(args.exclude or [])
    raw = getattr(args, "scope", None)
    if raw and (includes or excludes):
        raise CliError(
            "E_USAGE", "--scope 与 --include / --exclude 不能同时给",
            exit_code=EXIT_USAGE,
            hint='二选一：要写完整 scope 就用 --scope \'{"include":[…],"exclude":[…]}\'，'
                 "否则用 --include / --exclude（可重复）。")
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise CliError("E_BAD_SCOPE", f"--scope 不是合法 JSON：{exc}",
                           exit_code=EXIT_USAGE,
                           hint='例：--scope \'{"include":["*.example.com"],"exclude":[]}\'',
                           ) from exc
        if not isinstance(parsed, dict):
            raise CliError("E_BAD_SCOPE", "--scope 必须是一个 JSON 对象",
                           exit_code=EXIT_USAGE)
        unknown = sorted(set(parsed) - set(SCOPE_KEYS))
        if unknown:
            raise CliError(
                "E_BAD_SCOPE", f"--scope 里有本程序不认识的键：{', '.join(unknown)}",
                exit_code=EXIT_USAGE,
                hint=f"scope 只支持 {', '.join(SCOPE_KEYS)}；"
                     f"**不认识的键不会被忽略，而是会被拒绝** —— 一个存进去却"
                     f"永远不生效的 scope，比一句报错难查得多。",
                supported=list(SCOPE_KEYS), rejected=unknown)
        scope = parsed
    else:
        scope = {"include": includes, "exclude": excludes}
    for key in ("include", "exclude"):
        value = scope.get(key)
        if value is not None and not isinstance(value, list):
            raise CliError("E_BAD_SCOPE", f"scope.{key} 必须是字符串数组",
                           exit_code=EXIT_USAGE)
    return scope


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

#: `import` 的失败判定表：`(state, 退出码, error.code, 这是什么, 该怎么改)`。
#: **一处声明、一处执行** —— 加一档只改这一行，判定顺序就是它的顺序。
#:
#: `needs_confirmation` 排在最前：它要的是一个人**做决定**，另外两档要的只是
#: 一次修正。同一批文件里两者都有时，等你的那件事优先。
_IMPORT_VERDICTS = (
    ("needs_confirmation", EXIT_HUMAN, "E_PARSER_AMBIGUOUS",
     "没有解析器被采用（已进 Inbox 待人工确认）",
     "用 `--parser <工具名>` 指定一个解析器重跑，或先在画布 / Inbox 里裁决。"
     "**本命令不替你猜一个** —— 猜错的那一半不会有任何症状。"),
    ("failed", EXIT_DATA, "E_IMPORT_FAILED",
     "导入失败（读不到 / 解析崩了）",
     "看报告里这个文件的 `reason` / `parse_error`，以及 `files[].counts`。"
     "`state=failed` 的文件一行事实都没落，修好后重导是安全的。"),
    ("cancelled", EXIT_DATA, "E_IMPORT_CANCELLED",
     "尚未开始就被中断（已提交的部分保留，可 undo）",
     "`magpie jobs` 能看到这个作业；`magpie undo <job>` 可以撤销它。"),
)


#: 文件状态里哪些**这一次被正常受理了**（不构成失败）。与 `pipeline._PRODUCED_STATES`
#: 是同一条口径（`skipped` 也算 —— 幂等跳过意味着数据已经在库里了）。
#:
#: 唯一的例外是二进制那一支：它**也**报 `skipped`，但那是一次拒绝（见
#: `_import_verdict` 的 docstring）。所以判"受理了"必须同时看 state 与 parser，
#: 不能只做集合成员判断 —— 只判状态的话，一个被拒的 zip 会在"这一批还有谁
#: 有问题"的清单里被漏掉。
_ACCEPTED_STATES = frozenset({"done", "partial", "skipped"})


def _accepted(item: dict[str, Any]) -> bool:
    state = str(item.get("state"))
    if state == "skipped":
        return bool(item.get("parser"))
    return state in _ACCEPTED_STATES


def _others_note(files: list[dict[str, Any]], problems: list[str], *,
                 chosen: str) -> str:
    """追加给 `hint` 的一句：这一批里**还有谁**也有问题。

    一次导入可以同时有多种毛病，而退出码只能报**最需要人的**那一档（见
    `_IMPORT_VERDICTS` 的顺序）。于是这句话必须把其余的也说出去 —— 不说的
    话，"它报了 3，所以其余的都还好"会变成一个读得通但错的结论。

    "别的问题"有两种形状：某个文件的状态不对（`files`），以及某个路径根本
    没展开（`problems` —— 它不在 `files` 里）。少算后者的话，一次
    `magpie import 好文件.txt 打错的路径.txt` 会只讲前者。
    """
    bits = []
    others = [f for f in files if str(f.get("state")) != chosen and not _accepted(f)]
    if others:
        names = [Path(str(f.get("path"))).name for f in others]
        bits.append(f"{len(others)} 个文件也有问题"
                    f"（{'、'.join(names[:5])}"
                    + ("…" if len(names) > 5 else "") + "）")
    if problems:
        bits.append(f"{len(problems)} 个路径无法展开")
    if not bits:
        return ""
    # 起头一个换行：`--human` 把整条 hint 当作一行打出来，不换行的话这句话
    # 会粘在上一句的句尾上（换行在 JSON 字符串里也无害）。
    return ("\n  ⚠ 这一批里还有 " + "、".join(bits)
            + "。退出码只报最需要人的那一档，逐文件的完整状态见 error.report.files。")


def _import_verdict(report: dict[str, Any]) -> tuple[CliError | None, dict[str, Any]]:
    """导入报告 → (该抛的错误 or None, 结构化摘要)。

    `skipped` 的两种来源必须分开判，而它只有一个 state：

      * **二进制输入**（`_binary_report`）—— 输入根本没法解析，且**没有**进
        Inbox 等人工裁决。这是一次失败（退出码 2），不是"什么都没做"。
      * **幂等跳过**（`_already_imported`）—— 同一个 sha256 已由同一 fingerprint
        导入过，事实就在库里。这是一次**成功**，退出码 0。

    区分它们的不是文案，是结构：二进制那条报告的 `parser` 一定是 `None`
    （探测阶段就短路了，从来没有解析器被选中），幂等那条一定是一个
    `工具@版本` 指纹。按字符串匹配"二进制输入"那几个字迟早会漂。
    """
    files = list(report.get("files") or [])
    problems = [str(p) for p in (report.get("problems") or [])]

    def reason_of(item: dict) -> Any:
        return item.get("skip_reason") or item.get("reason") or item.get("parse_error")

    summary = {
        "files": [{"path": f.get("path"), "state": f.get("state"),
                   "job_id": f.get("job_id"), "reason": reason_of(f)}
                  for f in files],
        "problems": problems,
        "job_ids": list(report.get("job_ids") or []),
        "totals": dict(report.get("totals") or {}),
    }

    for state, exit_code, err_code, what, hint in _IMPORT_VERDICTS:
        hit = [f for f in files if str(f.get("state")) == state]
        if not hit:
            continue
        names = [Path(str(f.get("path"))).name for f in hit]
        extra: dict[str, Any] = {
            "report": summary,
            "files": [{"path": f.get("path"), "state": state, "reason": reason_of(f)}
                      for f in hit],
        }
        if state == "needs_confirmation":
            extra["candidates"] = _ranking_of(hit)
        hint = hint + _others_note(files, problems, chosen=state)
        return CliError(
            err_code,
            f"{len(hit)} 个文件{what}：{'、'.join(names[:5])}"
            + ("…" if len(names) > 5 else ""),
            exit_code=exit_code, hint=hint, **extra), summary

    # ② 二进制：state 也是 skipped，但没有任何解析器被选中 —— 见函数 docstring。
    binary = [f for f in files
              if str(f.get("state")) == "skipped" and not f.get("parser")]
    if binary:
        names = [Path(str(f.get("path"))).name for f in binary]
        return CliError(
            "E_IMPORT_BINARY",
            f"{len(binary)} 个文件是二进制，已跳过（不解析也不归档）："
            f"{'、'.join(names[:5])}" + ("…" if len(names) > 5 else ""),
            exit_code=EXIT_DATA,
            hint="二进制输入按设计**不进入解析**（`raw_files` 是'可复算原文的锚点'，"
                 "而这里没有任何事实会指向它 —— 建一个没有下游的归档行只会制造"
                 "孤儿）。压缩包请先解开再导。"
                 + _others_note(files, problems, chosen="skipped"),
            report=summary,
            files=[{"path": f.get("path"), "state": "skipped",
                    "reason": reason_of(f)} for f in binary]), summary

    # ③ 展开不开的路径（不存在 / 既不是文件也不是目录）。**响亮地失败**：
    #    "你给的 3 个路径里有 1 个不存在"被静默吞掉，最容易被误读成
    #    "那个目录里本来就没东西"。
    #
    #    排在上面两档**之后**，与 `_IMPORT_VERDICTS` 的顺序同一条理由：一个
    #    打错的路径是一次立刻能改的修正，而一个待裁决的文件要的是一个决定。
    #    两者同时出现时先报后者 —— 而前者靠 `_others_note` 出现在同一句提示里，
    #    不会消失。`chosen` 给的是一个**不可能是 state 的字符串**，好让所有
    #    有问题的文件都被算进"还有谁"。
    if problems:
        return CliError(
            "E_IMPORT_PATH",
            f"{len(problems)} 个路径无法展开：{problems[0]}"
            + (f"（共 {len(problems)} 个）" if len(problems) > 1 else ""),
            exit_code=EXIT_DATA,
            hint="路径必须存在，且是文件或目录。给目录时只挑扩展名在 "
                 f"{'、'.join(ingest.INPUT_SUFFIXES)} 里的文件"
                 "（不递归去认 .db / .png / 压缩包）；要导一个扩展名不在这个"
                 "清单里的文件，**直接把那个文件路径写出来**。"
                 + _others_note(files, [], chosen="__path__"),
            report=summary, problems=problems), summary

    return None, summary


def _ranking_of(hit: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`needs_confirmation` 的候选项 —— 直接取仲裁报告里的 `ranking`。

    §6.3 的信封里 `candidates` 就是为这种"需要人挑一个"准备的。**不在这里
    重新算一遍分数**：那是 `ingest.sniff_rank` 的事，两边各算一次就会分叉。
    """
    out: list[dict[str, Any]] = []
    for f in hit[:5]:
        ranking = ((f.get("arbitration") or {}).get("ranking")) or []
        out.append({"file": Path(str(f.get("path"))).name,
                    "reason": (f.get("arbitration") or {}).get("reason"),
                    "ranking": ranking})
    return out


def cmd_import(args, ctx) -> tuple[dict, list[str]]:
    """`magpie import <路径…> [--parser] [--source] [--dry-run]`。

    **走的是 `ingest.ingest_paths`** —— 与 `magpie app` 的 GUI 导入、2.5 的
    最小入口同一条实现。CLI 这一层只做四件事：解析出 `IngestOptions`、把
    `--dry-run` 透下去、把报告套进信封、按 `_import_verdict` 定退出码。
    """
    conn: sqlite3.Connection = ctx.conn()
    project_id = resolve_project(conn, args.project)
    data_dir = resolve_data_dir(getattr(args, "data_dir", None))
    archive = str(getattr(args, "archive", None) or "archived")

    opts = ingest.IngestOptions(
        project_id=project_id, data_dir=data_dir,
        parser=args.parser, source_name=args.source,
        authority_level=str(getattr(args, "authority", None) or "unverified"),
        archive=archive, dry_run=bool(args.dry_run), actor="cli",
        # 报告副本落进 `<data-dir>/reports/`（坏行明细本来就在那儿）。
        # `magpie doctor` 报"未知字段"只能靠它 —— 那个信号只活在一次解析里。
        report_dir=data_dir / "reports")

    report = ingest.ingest_paths(conn, opts, list(args.paths))
    err, summary = _import_verdict(report)

    notes: list[str] = []
    if not report["files"] and not report["problems"]:
        # 一个可导入的文件都没匹配到。**不是错误**（一个此刻为空的目录就是
        # 这种结果），但它必须**看得见** —— 脚本拿着一个成功的退出码往下走时，
        # "导了 0 个"与"导了一批"的唯一区别就是报告里这一点。
        notes.append(
            "没有匹配到任何可导入的文件。给目录时按扩展名过滤"
            f"（{'、'.join(ingest.INPUT_SUFFIXES)}）—— "        # noqa: SLF001
            "要导一个扩展名不在这个清单里的文件，直接写它的路径。")

    #  回显的输入路径也过掩码（4.2 判据 ①，向量 B）：`report` 里的 path 在
    #  `ingest_file` 已经掩过，但 JSON 信封的 `paths` 是从命令行实参直接来的
    #  —— 不掩它，`magpie import tok_ghp_….har` 的 stdout 就带着明文出去。
    paths = [str(normalize.redact_secrets(str(p))) for p in args.paths]
    data = {
        "kind": "import",
        "project_id": project_id,
        "paths": paths,
        "parser": args.parser,
        "source": args.source,
        "archive": archive,
        "data_dir": str(data_dir),
        "dry_run": bool(args.dry_run),
        "state": _summary_state(summary),
        "notes": notes,
        "report": report,
    }
    human = _import_human(data, summary)
    if err is not None:
        # 报告仍然原样交出去 —— 见模块 docstring 最后一段。
        err.extra.setdefault("report", summary)
        err.extra.setdefault("data_dir", str(data_dir))
        err.extra.setdefault("dry_run", bool(args.dry_run))
        raise err
    return data, human


def _summary_state(summary: dict[str, Any]) -> str:
    """整批的粗粒度状态。**逐字照抄 2.7 的 `ImportRegistry._task`**：

    `state` 这个字段的取值域属于 `/api/v1`（`queued/running/done/failed/
    cancelled`），CLI 不扩一个新值。所以"一个文件都没匹配到"也报 `done`
    —— 它是"这批做完了，什么都没做"，与 `cancelled`（被打断）是两回事。
    空批这件事由 `report.files == []` 与 `report.problems` 表达。

    它**不是** `_import_verdict`：那个决定退出码（"要不要响亮地失败"），这个
    只是报告里的一个字段。分开的理由是退出码的判定比 state 更细 ——
    二进制拒绝与文件真的坏了都落在 `failed` 档，但一个是本工具拒收、
    一个是上游的产物坏了。
    """
    states = [str(f.get("state")) for f in summary.get("files") or []]
    if states and all(s == "failed" for s in states):
        return "failed"
    return "done"


def _import_human(data: dict, summary: dict) -> list[str]:
    report = data["report"]
    counts = {"done": 0, "partial": 0, "skipped": 0, "failed": 0,
              "needs_confirmation": 0, "cancelled": 0}
    for f in report.get("files") or []:
        state = str(f.get("state"))
        counts[state] = counts.get(state, 0) + 1
    totals = report.get("totals") or {}
    head = f"{'（干跑）' if data['dry_run'] else ''}导入 → 项目 {data['project_id']}"
    lines = [
        head,
        f"  输入 {len(report.get('files') or [])} 个文件"
        f"（{'、'.join(f'{k} {v}' for k, v in counts.items() if v) or '一个都没有'}）",
        f"  档案区：{data['data_dir']}"
        f"（{'归档副本' if data['archive'] == 'archived' else '只记路径，不复制'}）"
        f"  parser={data['parser'] or '（自动仲裁）'}  source={data['source'] or '（取解析器的工具名）'}",
        "  计数：added {added}  updated {updated}  skipped {skipped}  "
        "conflicts {conflicts}  bad_lines {bad_lines}  unknown_fields {unknown_fields}"
        "  records {records}".format(**{k: totals.get(k, 0) for k in (
            "added", "updated", "skipped", "conflicts", "bad_lines",
            "unknown_fields", "records")}),
    ]
    for item in summary["files"]:
        mark = {"done": "✓", "partial": "~", "skipped": "·"}.get(item["state"], "✗")
        lines.append(f"  {mark} {Path(str(item['path'])).name}"
                     f"  [{item['state']}]"
                     + (f"  job {item['job_id']}" if item["job_id"] else "")
                     + (f"  {item['reason']}" if item["reason"] else ""))
    for problem in summary["problems"]:
        lines.append(f"  ✗ {problem}")
    for note in data.get("notes") or []:
        lines.append(f"  ⚠ {note}")
    if data["dry_run"]:
        lines.append("  （干跑：事实一行未写，事务已 ROLLBACK）")
    return lines


# ---------------------------------------------------------------------------
# undo / redo
# ---------------------------------------------------------------------------


def _undo_redo(args, ctx, *, op: str) -> tuple[dict, list[str]]:
    """`undo` / `redo` 的公共体。**两者只有 `ops` 里调哪个函数不同。**

    刻意不把它们写成两个几乎一样的函数：差异只有一行，而任何"另一处也要改"
    的地方（比如以后加 `--actor`）都会变成两处。
    """
    conn: sqlite3.Connection = ctx.conn()
    job_id = _positive_int(args.job, what=f"{op} 的 <job>")
    job = _job_row(conn, job_id)
    dry = bool(args.dry_run)

    fn = ops.undo if op == "undo" else ops.redo
    report = fn(conn, job_id, actor="cli", dry_run=dry)

    data = {"op": op, "job_id": job_id, "dry_run": dry, "report": report}
    return data, _undo_redo_human(data, job)


def _undo_redo_human(data: dict, job: dict) -> list[str]:
    report = data["report"]
    op, dry = data["op"], data["dry_run"]
    impact = report.get("impact") or {}
    verb = "撤销" if op == "undo" else "复原"
    lines = [
        f"{'（干跑）' if dry else ''}{verb}作业 {data['job_id']}"
        f"（{job['kind']}，state={job['state']}，project {job['project_id']}）",
        f"  已应用：{'是' if job['applied'] else '否'} → "
        f"执行后：{'否' if op == 'undo' else '是'}"
        + ("（干跑不改动，这只是它会得到的结果）" if dry else ""),
    ]
    if op == "undo":
        lines += [
            f"  贡献 {impact.get('contributions', 0)} 条全部追加 retract 抑制"
            f"（本次新增抑制 {impact.get('superseded', 0)} 条）",
            f"  撤销 alias {impact.get('alias_revoked', 0)} 条 / "
            f"merge 成员 {impact.get('merge_members_revoked', 0)} 条",
            f"  关闭 inbox {impact.get('inbox_closed', 0)} 项 / "
            f"打开孤儿项 {impact.get('inbox_opened', 0)} 项",
            f"  可物理清理（GC）：{len(impact.get('purge_eligible') or [])} 个",
        ]
    else:
        if report.get("noop"):
            lines.append("  这个作业本来就是生效状态，没有任何撤销边要解除"
                         "（幂等：不是错误）")
        lines += [
            f"  解除 retract 抑制 {impact.get('supersessions_lifted', 0)} 条",
            f"  恢复 alias {impact.get('alias_restored', 0)} 条 / "
            f"merge 成员 {impact.get('merge_members_restored', 0)} 条",
            f"  重开 inbox {impact.get('inbox_reopened', 0)} 项 / "
            f"关闭 {impact.get('inbox_closed', 0)} 项",
        ]
        blocked = impact.get("blocked") or []
        if blocked:
            # `applied=0` + `blocked` 在 /api/v1 里是 **200**（"部分成功"是一个
            # 结果，不是一个错误），所以这里也退出 0 —— 但绝不能安静。
            lines.append(
                f"  ⚠ 有 {len(blocked)} 条 alias 被更新的身份决定占用了，"
                f"未能恢复 —— 该作业现在 **applied=0**（不算完全复原）。"
                f"人工处置后再 redo 一次。")
    lines.append(f"  不变量校验：{'通过' if (report.get('integrity') or {}).get('ok') else '**未通过**'}")
    return lines


def cmd_undo(args, ctx) -> tuple[dict, list[str]]:
    """`magpie undo <job> [--dry-run]` —— 撤销一个作业（↔ `POST /jobs/{id}/undo`）。"""
    return _undo_redo(args, ctx, op="undo")


def cmd_redo(args, ctx) -> tuple[dict, list[str]]:
    """`magpie redo <job> [--dry-run]` —— 复原一个被撤销的作业（↔ `POST /jobs/{id}/redo`）。"""
    return _undo_redo(args, ctx, op="redo")


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------


def cmd_jobs(args, ctx) -> tuple[dict, list[str]]:
    """作业历史。不给 id 时是列表（**每条都带 `explain_job_scope`**），
    给了 id 时是单条详情（与 `GET /jobs/{id}` 同形：含 activity）。

    `explain_job_scope` 逐条带上而不是另算一个摘要：`interpret_with` 判的是
    "这个作业该用**当时的** scope 还是当前 scope 来解释"，而判错的后果是一个
    看起来很有道理的假结论（"这条覆盖已经失效"）。它只有一份实现，在这里
    再写一遍就是第二个真相。
    """
    conn: sqlite3.Connection = ctx.conn()
    if args.job is not None:
        return _job_detail(conn, _positive_int(args.job, what="jobs 的 <job>"))
    return _job_list(conn, args)


def _job_detail(conn: sqlite3.Connection, job_id: int) -> tuple[dict, list[str]]:
    job = _decorate_job(conn, _job_row(conn, job_id))
    scope = ops.explain_job_scope(conn, job_id)
    activity = [dict(r) for r in conn.execute(
        "SELECT id, at, actor, action, target_kind, target_id, detail"
        " FROM activity_log WHERE job_id=? ORDER BY id DESC LIMIT 50", (job_id,))]
    for item in activity:
        item["detail"] = _json_or_none(item["detail"])
    data = {"job": job, "scope": scope, "activity": activity,
            "can_undo": job["can_undo"], "can_redo": job["can_redo"]}
    counts = job["counts"]
    human = [
        f"作业 {job['id']}  {job['kind']}  state={job['state']}"
        f"  applied={'是' if job['applied'] else '否'}"
        f"  （{'可 undo' if job['can_undo'] else '可 redo'}）",
        f"  项目 {job['project_id']}  actor={job['actor']}",
        f"  started={job['started_at']}  finished={job['finished_at']}",
        f"  coverage={job['coverage']}  parser={job['parser_fingerprint']}",
        f"  贡献 {counts['contributions']} 条，其中已撤回 {counts['retracted']} 条",
        f"  scope：用 {'当时的（historical_scope）' if scope['interpret_with'] == 'historical_scope' else '当前的'} 解释"
        f"（job v{scope['job_scope_version']} / 当前 v{scope['current_scope_version']}）"
        + (f"  ⚠ 落后 {scope['scope_versions_behind']} 版"
           if scope["scope_versions_behind"] else "")
        + ("  ⚠ parser 指纹已漂移" if scope["parser_drift"] else ""),
        f"  该 scope 能否用于 inactive 判定：{'可以' if scope['coverage_usable_for_inactive'] else '**不可以**'}",
        f"  job_scope={_json_or_none(scope.get('job_scope'))}",
        "",
        "活动：",
    ]
    human += table(["ID", "AT", "ACTOR", "ACTION", "TARGET"],
                   [[a["id"], a["at"], a["actor"], a["action"],
                     f"{a['target_kind']}:{a['target_id']}"] for a in activity]) \
        if activity else ["  （无）"]
    return data, human


def _job_list(conn: sqlite3.Connection, args) -> tuple[dict, list[str]]:
    state = getattr(args, "state", None)
    if state is not None and state not in JOB_STATES:
        raise CliError("E_BAD_STATE", f"state 只能是 {' / '.join(JOB_STATES)}，收到 {state!r}",
                       exit_code=EXIT_USAGE, supported=list(JOB_STATES))
    applied = _tri_state(getattr(args, "applied", None))
    limit = _limit(getattr(args, "limit", DEFAULT_JOB_LIMIT), ceiling=MAX_JOB_LIMIT)

    where = ["1=1"]
    params: list[Any] = []
    project_spec = getattr(args, "project", None)
    if project_spec is not None:
        where.append("project_id=?")
        params.append(resolve_project(conn, project_spec))
    if state is not None:
        where.append("state=?")
        params.append(str(state))
    if applied is not None:
        where.append("applied=?")
        params.append(1 if applied else 0)

    rows = conn.execute(f"SELECT {_JOB_COLUMNS} FROM jobs"
                        f" WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
                        (*params, limit)).fetchall()
    items = []
    for row in rows:
        item = _decorate_job(conn, dict(row))
        # scope 逐条带上：作业解释只有 `ops.explain_job_scope` 一份实现。
        # 这里的 N 是 `--limit`（默认 100），每条是两次主键查找 —— 用 N+1 换
        # "解释只有一份"是划算的，反过来（另写一个批量摘要）才有第二个真相。
        item["scope"] = ops.explain_job_scope(conn, int(item["id"]))
        items.append(item)

    data = {"jobs": items, "count": len(items), "limit": limit,
            "filters": {"state": state, "applied": applied,
                        "project": project_spec}}
    if len(items) == limit:
        notes = [f"已到 --limit {limit}，可能还有更早的作业没列出来（调大 --limit，"
                 f"上限 {MAX_JOB_LIMIT}）。"]
    else:
        notes = []
    data["notes"] = notes
    human = [f"作业历史（最近的在前）：共 {len(items)} 条"
             + (f"，已达 --limit {limit}" if len(items) == limit else "")]
    human += table(
        ["ID", "PROJ", "KIND", "STATE", "APPLIED", "可撤", "CONTRIB", "撤回", "SCOPE", "STARTED"],
        [[j["id"], j["project_id"], j["kind"], j["state"],
          "是" if j["applied"] else "否",
          "undo" if j["can_undo"] else "redo",
          j["counts"]["contributions"], j["counts"]["retracted"],
          j["scope"]["interpret_with"].replace("_scope", ""),
          j["started_at"]] for j in items]) \
        if items else ["  （（无）—— 这个项目还没有任何作业。`magpie import` 会产生它们。）"]
    for note in notes:
        human.append(f"  ⚠ {note}")
    return data, human


def _tri_state(raw: Any) -> bool | None:
    """`--applied` 的三态：不给 = 全都要，`true/false/1/0` = 只看一侧。

    刻意收下布尔字符串自己判，而不是让 argparse 用 `type=bool` —— 后者把
    `"false"` 也解析成 True（非空字符串恒真），而那是一个**静默的相反结论**。
    """
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "y"):
        return True
    if text in ("0", "false", "no", "n"):
        return False
    raise CliError("E_BAD_APPLIED",
                   f"--applied 只收 true/false（或 1/0），收到 {raw!r}",
                   exit_code=EXIT_USAGE)


__all__ = ["WRITE_COMMANDS", "cmd_import", "cmd_jobs", "cmd_project_create",
           "cmd_redo", "cmd_undo"]
