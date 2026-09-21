"""项目 scope 的求值：一个实体的键**落在范围里，还是越界**（§5.2 / §7.4 的「越界资产」）。

为什么这个模块现在才有
----------------------
`projects.scope` 从 001 起就是 `{include: [...], exclude: [...]}` 的形状，
`scope_hash` 把它连同 parser 指纹一起冻进每个作业的审计（审阅点 7）。但**"匹配"
这一半一直没人写** —— 于是两件事同时悬着：

  * 2.7 的 `/entities?in_scope=` 只能**响亮地拒绝**（`E_UNSUPPORTED_FILTER`）；
  * 体检页的「越界资产」一直列在「尚未提供」里（`DoctorView.vue` 的
    `NOT_PROVIDED`），因为没有任何东西能回答"哪些资产越界了"。

3.6 要把「越界资产」变成一个能被人看见的数字，就必须有人回答这个。而它**只能
有一处**：读侧的具名查询（`query.out_of_scope_entities`）与写侧的 Inbox 生产者
（`ops.refresh_scope_drift`）各写一份判据，就会在大小写、子域、CIDR 这三件事上
分叉 —— 分叉之后没有答案的是"哪个才对"，而这一页的全部意义就是让人相信它。

判据（三条，都写在这里，别处不许再猜）
--------------------------------------
1. **只有资产类 kind 参与判定**（`domain` / `ip` / `cidr`）。`product` / `apischema`
   之类的键没有地址语义 —— "产品 nginx 在不在我的网段范围内"是一个**没有答案**
   的问题，所以答案是 `None`（不问），不是 `False`（越界）。把不问当成越界，
   会让体检页在任何一份导入之后都凭空多出一批待办。

2. **include 非空 = 白名单**：不在名单里就是越界。include 为空 = 没有约束
   （**全在范围内**）。这一条必须写死并且写在显眼处：反过来读（空 = 全越界）
   会让每个没配 scope 的项目在第一次导入时就刷出一屏越界项，而那些项是假的。

3. **exclude 优先于 include**：命中 exclude 就是越界，哪怕 include 也命中。
   白名单说的是"我想看哪些"，黑名单说的是"这些别管" —— 后者的语气更强。

匹配规则（判据是**键的形状**，不是猜）
--------------------------------------
  * `*.example.com` —— 读作 **`example.com` 及其全部子域**（`api.example.com`
    命中，`example.com` 自己也命中）。这一条**故意不同于 fnmatch 的字面语义**
    （fnmatch 里 `*.example.com` 不含 `example.com`），理由是具体的：解析器
    会从 `api.example.com` 自动推出 apex `example.com`（domainlist / httpx 都
    这么做），于是字面读法会让**每一次导入**都多报一条"apex 越界" —— 那不是
    一个发现，那是一句纯噪音，而噪音会让人连真发现一起忽略。
  * `10.0.0.0/24` —— 网段包含：`ip` 落在里面算命中，`cidr` 是它的子网也算命中。
  * 其余按 fnmatch 字面通配（`example.com` 只匹配它自己，`h1.example.com` 只匹配
    那一台）。

大小写一律按 `.lower()` 折叠后再比，且**不用 `fnmatch.fnmatch`** —— 它在
Windows 上会把两边都 casefold，于是同一份 scope 在不同机器上给出两套答案。
判据必须与平台无关，否则"越界资产 3"这个数字换台机器就变了。

`ipaddress` / `fnmatch` 都在标准库里，所以本模块不引入任何依赖（R6）。
"""

from __future__ import annotations

import fnmatch
import ipaddress
from typing import Any

#: 参与 scope 判定的 kind —— **唯一声明处**（见模块文档第 1 条）。
#: 这三个是键里带地址语义的全部：域名、单个地址、网段。
SCOPE_KINDS = ("domain", "ip", "cidr")


def patterns_of(scope: Any) -> tuple[list[str], list[str]]:
    """scope → (include, exclude)。两种形状都收（dict / None / 别的），
    读不懂的一律当**空**（= 没有约束）。

    读不懂时当空而不是当全越界：`scope` 是一列 JSON，坏数据由体检的不变量那一栏
    去报；让一个坏 scope 把每个导入都变成一屏假待办，是把一个数据问题放大成
    一个噪音问题。
    """
    if not isinstance(scope, dict):
        return [], []
    out: list[str] = []
    for key in ("include", "exclude"):
        value = scope.get(key)
        if isinstance(value, (list, tuple)):
            out.append([str(x) for x in value])
        else:
            out.append([])
    return out[0], out[1]


def _as_network(pattern: str) -> ipaddress._BaseNetwork | None:  # noqa: SLF001
    """pattern 是不是一个网段；不是就回 None（它按域名/字面通配处理）。"""
    try:
        return ipaddress.ip_network(str(pattern).strip(), strict=False)
    except ValueError:
        return None


def _of_class(patterns: list[str], kind: str) -> list[str]:
    """名单里**对这一类资产表了态**的那些条目。

    两类是分开的：域名模式（`*.example.com`）管 `domain`，网段（`10.0.0.0/24`）管
    `ip` / `cidr`。混着拿会出事 —— 一个只写了域名的 scope 并没有对地址表态，
    而 httpx 会为每台在范围内的主机写一条 A 记录实体；把那些地址判成"越界"，
    一次导入就能刷出一屏待办，而它们没有一条是发现。
    """
    if kind in ("ip", "cidr"):
        return [p for p in patterns if _as_network(p) is not None]
    return [p for p in patterns if _as_network(p) is None]


def _matches(pattern: str, kind: str, key: str) -> bool:
    """一个 pattern 对一个键。`kind` 决定要不要先试网段包含。"""
    text = str(pattern).strip()
    if not text:
        return False
    if kind in ("ip", "cidr"):
        network = _as_network(text)
        if network is not None:
            try:
                if kind == "ip":
                    return ipaddress.ip_address(str(key)) in network
                block = ipaddress.ip_network(str(key), strict=False)
                return block == network or block.subnet_of(network)
            except ValueError:                  # pragma: no cover - 坏键
                return False
    low_key, low_pattern = str(key).lower(), text.lower()
    if fnmatch.fnmatchcase(low_key, low_pattern):
        return True
    # `*.example.com` 也收 apex 自己（见模块文档的匹配规则）。
    return low_pattern.startswith("*.") and low_key == low_pattern[2:]


def in_scope(scope: Any, kind: str, key: str) -> bool | None:
    """键在不在范围里。`None` = **不问**（不是越界，也不是在范围内）。

    三态是刻意的：调用方必须对"不问"做一次显式决定，而不是被 `False` 顺手
    当成"越界"。体检页的「越界资产」与 Inbox 的 `scope_drift` 都只收 `False`。
    """
    kind = str(kind)
    if kind not in SCOPE_KINDS:
        return None
    include, exclude = patterns_of(scope)
    if not include and not exclude:
        return None                             # 没配 scope = 没有约束
    include, exclude = _of_class(include, kind), _of_class(exclude, kind)
    if not include and not exclude:
        return None                             # 名单对**这一类**没有表态
    for pattern in exclude:
        if _matches(pattern, kind, key):
            return False
    if not include:
        return True                             # 只有黑名单，且没命中
    return any(_matches(pattern, kind, key) for pattern in include)


def describe(scope: Any) -> dict[str, Any]:
    """越界项 payload 里带的那一份 scope 摘要。

    带上它是因为"越界"这个判断只有在**对着哪份名单**说的时候才 actionable：
    一条只说"10.0.0.7 越界"的待办，人还得回去翻项目 scope 才知道改哪个名单。
    """
    include, exclude = patterns_of(scope)
    return {"include": include, "exclude": exclude}


__all__ = ["SCOPE_KINDS", "describe", "in_scope", "patterns_of"]
