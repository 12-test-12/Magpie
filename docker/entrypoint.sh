#!/bin/sh
# 容器入口：socat 回环转发 + magpie app。设计三条见 Dockerfile 文件头，这里只
# 重复与端口有关的那条：本体只绑 127.0.0.1（产品红线，镜像不改），对外的
# 0.0.0.0 由下面这条 socat 承担 —— 两端口不能同号，否则互相占位。
#
# 带参数执行时原样透传（docker run <image> magpie ls 之类的旁路），
# 不起服务也不起转发。
set -eu

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

: "${MAGPIE_PORT:=8000}"           # socat：容器内 0.0.0.0
: "${MAGPIE_APP_PORT:=8001}"       # magpie app：127.0.0.1（必须与上面不同号）
: "${MAGPIE_DATA_DIR:=/data}"
: "${MAGPIE_LOG_LEVEL:=warning}"

socat TCP-LISTEN:"$MAGPIE_PORT",fork,reuseaddr,bind=0.0.0.0 \
      TCP:127.0.0.1:"$MAGPIE_APP_PORT" &

cat <<EOF
magpie 容器：
  页面 http://localhost:${MAGPIE_PORT}/?token=<token>
  token 见下面 magpie app 打印的 URL（每次启动随机）；设 MAGPIE_TOKEN 可固定它
  数据 ${MAGPIE_DATA_DIR}（建议挂成卷）
EOF

set -- magpie app \
  --host 127.0.0.1 \
  --port "$MAGPIE_APP_PORT" \
  --data-dir "$MAGPIE_DATA_DIR" \
  --log-level "$MAGPIE_LOG_LEVEL" \
  --no-browser
if [ -n "${MAGPIE_TOKEN:-}" ]; then
  set -- "$@" --token "$MAGPIE_TOKEN"
fi
exec "$@"
