#!/usr/bin/env bash
# ============================================================
# GitHub 自动同步脚本（不含明文 token）
# 用法: bash sync_github.sh "提交说明"
#   1) 从本地 .env 读取 GITHUB_PAT（不入库）
#   2) git add -A && commit && push（触发 GitHub Actions 自动构建新镜像）
#   3) 飞牛侧: docker compose pull && up -d 即完成升级
# 新对话接手：运行本脚本前先确认本地 .env 的 GITHUB_PAT 仍有效。
# ============================================================
set -e
cd "$(dirname "$0")"

MSG="${1:-update: 舆情分析系统同步}"
BRANCH="main"
REPO_URL="https://github.com/ruoshui6662/miaoyu-analysis.git"

# 从 .env 读取令牌（不入库，避免 GitHub secrets 扫描拦截明文 PAT）
PAT=""
for f in .env ../.env; do
  if [ -f "$f" ]; then
    PAT=$(grep -E '^GITHUB_PAT=' "$f" | head -1 | cut -d'=' -f2- | tr -d '\r')
    [ -n "$PAT" ] && break
  fi
done
if [ -z "$PAT" ]; then
  echo "[错误] 本地 .env 中未找到 GITHUB_PAT（无法推送）"
  echo "       请先配置：echo 'GITHUB_PAT=ghp_xxx' >> .env"
  exit 1
fi

echo "[1/3] 暂存并提交..."
git add -A
git commit -m "$MSG" >/dev/null 2>&1 || echo "       （无变更可提交）"

echo "[2/3] 推送 ${BRANCH} → ${REPO_URL}"
git push "https://ruoshui6662:${PAT}@${REPO_URL#https://}" "$BRANCH"

echo "[3/3] 完成：GitHub Actions 正在构建 ghcr.io/ruoshui6662/miaoyu-analysis:latest"
echo "      飞牛升级：docker compose pull && docker compose up -d"