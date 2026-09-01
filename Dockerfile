# 阶段构建用：Node 20 LTS（docx 生成）从官方 tarball 安装，避免 apt 旧版本
# 详见 Dockerfile

# ---- 构建期：仅用于 npm 安装 docx ----
FROM alpine:3.20 AS node-stage
RUN apk add --no-cache nodejs npm tzdata
# 国内镜像加速（可改回官方源）
RUN npm config set registry https://registry.npmmirror.com
COPY backend/scripts/package.json /build/package.json
RUN cd /build && npm install --omit=dev && node -e "require('docx'); console.log('docx OK')"

# ---- 运行期 ----
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    PIP_NO_CACHE_DIR=1 \
    NODE_PATH=/usr/local/lib/node_modules

WORKDIR /app

# 1) Node 运行时（docx 生成）——从官方 tarball 安装 v20，不依赖 apt 旧版
RUN apt-get update && apt-get install -y --no-install-recommends curl xz-utils ca-certificates \
    && curl -fsSL https://nodejs.org/dist/v20.18.0/node-v20.18.0-linux-x64.tar.xz \
       | tar -xJ -C /usr/local --strip-components=1 \
    && node --version && npm --version \
    && rm -rf /var/lib/apt/lists/*

# 2) Python 依赖（阿里云 PyPI 镜像，国内构建加速；可按需改回官方源）
COPY backend/requirements.txt /tmp/req.txt
RUN pip install -r /tmp/req.txt -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com

# 3) 应用代码 + 前端 + 环境模板
COPY backend /app/backend
COPY frontend /app/frontend
COPY .env.example /app/.env.example

# 4) docx 生成依赖（从构建期 node-stage 拷贝，避免重复 npm install）
COPY --from=node-stage /build/node_modules /app/backend/scripts/node_modules

# 5) 数据目录
RUN mkdir -p /app/data/tasks /app/data/reports /app/data/raw

VOLUME ["/app/data"]
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:5000/healthz >/dev/null || exit 1

CMD ["bash", "/app/backend/docker_entrypoint.sh"]