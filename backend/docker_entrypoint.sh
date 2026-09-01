#!/usr/bin/env bash
# 容器入口：首次启动初始化 .env 与数据目录，然后启动服务。
set -e

cd /app

# 首次启动：若无 .env 则从模板生成（容器内默认配置可直接运行采集，
# AI key 需通过环境变量或挂载 .env 提供）
if [ ! -f .env ]; then
  cp .env.example .env
  echo "[entrypoint] 已生成默认 .env（AI Key 为空，可后续配置）"
fi

mkdir -p /app/data/tasks /app/data/reports /app/data/raw

echo "[entrypoint] 启动舆情分析服务..."
exec python /app/backend/app.py --host 0.0.0.0 --port 5000