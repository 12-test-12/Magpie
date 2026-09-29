"""对外传输适配层（5.1 起）。**这里只有 stdio，没有任何网络传输。**

`magpie.core` 是数据层，`magpie.cli` / `magpie.api` 是同一份数据的两个人机入口，
`magpie.adapters` 是第三个：给 agent 用的 Model Context Protocol 入口。它遵守的
纪律与另外两个入口相同 —— 读侧复用 `core/query.py` / `cli/read.py` 的唯一实现、
信封与 `--json` / `/api/v1` 逐字同形、只读连接由 `PRAGMA query_only` 在数据库
那一侧执行（红线 R7：本包不发起任何出站请求）。

主体在 `magpie.adapters.mcp`；本包刻意不 eager import 它（`import magpie.adapters`
应当廉价，与包根 `magpie/__init__.py` 同一条理由）。
"""
