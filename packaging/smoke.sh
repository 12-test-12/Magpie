#!/usr/bin/env bash
# 打包冒烟：对**解压后的分发目录**跑（不是对 build 目录 —— 要证明的是拿到手
# 的那个压缩包能用）。CI 与本地同一份脚本，LARI-48 判据 2/3/4 的冒烟半边：
#
#   1. 只读命令活着：`magpie --version` 退出 0；若给了 MAGPIE_EXPECTED_VERSION
#      则逐字比对 —— 抓"PyInstaller 收错了一份陈旧安装"这类静默错。
#   2. schema 打包到位：`magpie init` 在临时数据区把全部迁移跑完（读的正是包内
#      `_internal/schema/`），随后只读的 `magpie schema --json` 退出 0，
#      且解析器清单**非空**（`"tool"` 至少一处）—— 这一条钉的是 pkgutil
#      发现机制在冻结态没哑（magpie.spec 里解析器文件副本存在的理由）。
#   3. 一条命令起服务：`magpie app` 回环监听；`/healthz`、`/`（GUI 静态页真身，
#      断言 `id="app"`）、`/openapi.json`（含 /api/v1）三发全 200。
#   4. 零外联：全程只打 127.0.0.1（R7 的回环口径）；另对包内 GUI 静态资源做
#      外部域名 grep（浏览器发起的出站与 Python 出站一样是出站，R7-ASSETS 同款
#      判据）。冒烟不依赖任何外网才能过的断言。
#
# 用法: packaging/smoke.sh <解压后的分发目录>   （目录里应有 magpie[.exe]）
set -euo pipefail

DIR="${1:?用法: packaging/smoke.sh <解压后的分发目录>}"

if [ -f "$DIR/magpie.exe" ]; then
  BIN="$DIR/magpie.exe"
elif [ -f "$DIR/magpie" ]; then
  BIN="$DIR/magpie"
else
  echo "!! 分发目录里找不到可执行入口: $DIR" >&2
  exit 1
fi

WORK="$(mktemp -d)"
APP_PID=""

cleanup() {
  # 一律用 `kill`（bash 自己 fork 的 pid）—— **不用 taskkill $!**：Git Bash 的
  # `$!` 是 MSYS 侧的 pid，与 Windows 真实 PID 不同（实测 $!=2021、tasklist
  # =3728），拿它喂 taskkill 只会得到一个被吞掉的 128，进程活着、SQLite 文件
  # 占着，冒烟明明全过却以 rm 的失败码收场。MSYS 的 kill 认得自己的映射。
  if [ -n "$APP_PID" ]; then
    kill "$APP_PID" 2>/dev/null || true
    # 强杀后文件句柄的释放与 rm 之间有窗口 —— 重试几拍，清不掉只警告不翻红：
    # 断言早已全部通过，清理是台架的家务，不该把绿改写成红。
    for _ in 1 2 3 4 5; do
      rm -rf "$WORK" 2>/dev/null && return 0
      sleep 0.3
    done
    echo "警告: 临时目录没清干净: $WORK" >&2
  else
    rm -rf "$WORK" 2>/dev/null || true
  fi
}
trap cleanup EXIT

echo "== 1) 只读命令: --version =="
VERSION_OUT="$("$BIN" --version)"
echo "   $VERSION_OUT"
case "$VERSION_OUT" in
  magpie\ [0-9]*.[0-9]*.[0-9]*) : ;;
  *) echo "!! --version 输出形状不对: $VERSION_OUT" >&2; exit 1 ;;
esac
if [ -n "${MAGPIE_EXPECTED_VERSION:-}" ]; then
  if [ "$VERSION_OUT" != "magpie $MAGPIE_EXPECTED_VERSION" ]; then
    echo "!! 版本与仓库源码不一致: 包内 '$VERSION_OUT' vs 期望 'magpie $MAGPIE_EXPECTED_VERSION'" >&2
    exit 1
  fi
fi

echo "== 2) schema 迁移（读包内 _internal/schema/）+ 解析器清单非空 =="
"$BIN" init --data-dir "$WORK/data"
"$BIN" schema --data-dir "$WORK/data" > "$WORK/schema.json"
if ! grep -q '"tool"' "$WORK/schema.json"; then
  echo "!! 解析器清单为空 —— pkgutil.iter_modules 在冻结态没扫到东西（见 magpie.spec 注释）" >&2
  head -c 2000 "$WORK/schema.json" >&2 || true
  exit 1
fi

echo "== 3) 一条命令起服务（只绑回环）=="
"$BIN" app --data-dir "$WORK/data" --no-browser > "$WORK/app.log" 2>&1 &
APP_PID=$!

PORT=""
for _ in $(seq 1 300); do
  PORT="$(sed -n 's|.*URL: http://127\.0\.0\.1:\([0-9][0-9]*\)/.*|\1|p' "$WORK/app.log" | head -1 || true)"
  [ -n "$PORT" ] && break
  sleep 0.2
done
if [ -z "$PORT" ]; then
  echo "!! 60s 内没等到服务的 URL 行，app 日志:" >&2
  cat "$WORK/app.log" >&2 || true
  exit 1
fi
echo "   回环端口: $PORT"

curl -fsS "http://127.0.0.1:$PORT/healthz" > "$WORK/healthz.json"
grep -q '"ok"' "$WORK/healthz.json" || { echo "!! /healthz 没有 ok" >&2; exit 1; }

curl -fsS "http://127.0.0.1:$PORT/" > "$WORK/index.html"
grep -q 'id="app"' "$WORK/index.html" \
  || { echo "!! / 不是 GUI 静态页 —— 包内 magpie/api/web/ 缺失？" >&2; exit 1; }

curl -fsS "http://127.0.0.1:$PORT/openapi.json" > "$WORK/openapi.json"
grep -q '"/api/v1/' "$WORK/openapi.json" \
  || { echo "!! /openapi.json 里没有 /api/v1 路由" >&2; exit 1; }

echo "== 4) 产物内 GUI 静态资源零出站（外部域名 grep）=="
WEB="$DIR/_internal/magpie/api/web"
if [ ! -d "$WEB" ]; then
  echo "!! 包内找不到 GUI 静态资源目录: $WEB" >&2
  exit 1
fi
HITS="$(grep -rEo '(https?:)?//[A-Za-z0-9._-]+\.[A-Za-z]{2,}' "$WEB" 2>/dev/null \
        | grep -v 'www\.w3\.org' || true)"
if [ -n "$HITS" ]; then
  echo "!! 产物 GUI 静态资源引用了外部域名（浏览器会替它发起出站）:" >&2
  echo "$HITS" >&2
  exit 1
fi

echo "== 冒烟通过: $BIN =="
