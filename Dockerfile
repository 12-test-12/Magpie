# Magpie 的容器分发 —— 与 PyInstaller 包（packaging/magpie.spec）并列的第二种
# "别人拿去就能跑"的形态。改之前先读三条：
#
#   1. **产品红线不动**：`magpie app` 只绑回环（api/app.py 的 serve() 在绑定前
#      拒绝非回环 host），镜像不去改它。容器内对外的暴露由
#      docker/entrypoint.sh 里的 socat 把 0.0.0.0:8000 转发到 127.0.0.1:8001
#      承担 —— 暴露与否仍是 `docker -p` 那个显式决定，token 鉴权在应用层全链路
#      照旧。两个端口必须不同号：0.0.0.0:8000 与 127.0.0.1:8000 互相占位
#      （EADDRINUSE），谁也绑不上。
#   2. **不需要 node**：前端构建产物已入库（magpie/api/web/，改前端要先
#      `npm run build` 再提交产物）—— 与 CI 的「产物逐字节一致」是同一条纪律。
#   3. **构建期网络 ≠ 运行时出站**（判据 4）：apt / pip 只在 build 里出现；
#      运行时只有回环（HEALTHCHECK 打 127.0.0.1 的 /healthz）与产品自身的
#      零出站红线。
#
# 基础镜像钉 3.13：与 CI 的 check.py / PyInstaller 打包同一个解释器大版本。

FROM python:3.13-slim

# socat 只干一件事：entrypoint.sh 里那条 0.0.0.0 → 127.0.0.1 的转发。
RUN apt-get update \
    && apt-get install -y --no-install-recommends socat \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# .dockerignore 是白名单式：上下文里只有下面这几样（仓库根的证据文件 /
# dist / 前端源码一概不进上下文 —— 构建产物在 magpie/api/web/ 里，已随 COPY 进来）。
COPY pyproject.toml README.md ./
COPY magpie ./magpie

# extras 与 CI 的 package job 逐字一致（api = fastapi+uvicorn，api-yaml = pyyaml）。
# 装完把源码删掉，镜像里只留 site-packages 与入口脚本。
RUN python -m pip install --no-cache-dir ".[api,api-yaml]" \
    && rm -rf /build

COPY docker/entrypoint.sh /usr/local/bin/magpie-entrypoint.sh

# 非 root + 固定的数据挂载点（卷挂 /data：数据库与数据区都在这下面）。
# 用 sh 显式起入口（ENTRYPOINT 见文件末尾）而不依赖 shebang + 执行位：
# Windows 上 checkout 出来的上下文没有可靠的 +x 位。
RUN useradd --system --uid 1000 --home-dir /data --shell /usr/sbin/nologin magpie \
    && mkdir -p /data \
    && chown magpie:magpie /data \
    && chmod 755 /usr/local/bin/magpie-entrypoint.sh

USER magpie
WORKDIR /data

ENV MAGPIE_PORT=8000 \
    MAGPIE_APP_PORT=8001 \
    MAGPIE_DATA_DIR=/data

EXPOSE 8000

# 走 socat（而不是直连 8001）—— 探的就是用户真正访问的那条路径，转发挂了
# 会如实变红。/healthz 无 token（create_app 里它挂在 router 之外），且打回环。
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request as u; u.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('MAGPIE_PORT', '8000'), timeout=3)"

# 脚本最后一行 exec magpie app：PID 1 就是本体，Ctrl-C / docker stop 直达。
ENTRYPOINT ["/bin/sh", "/usr/local/bin/magpie-entrypoint.sh"]
