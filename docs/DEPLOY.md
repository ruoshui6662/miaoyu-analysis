# 妙舆 · Docker 部署指南（飞牛 fnOS / 通用）

> 对应开发手册 A1a（最小容器化）。目标环境：飞牛 fnOS（支持镜像导入）或任意 Docker 主机。
> 部署后浏览器访问 `http://<主机IP>:5000`，本机默认 `http://localhost:5000`。

---

## 0. 两种部署方式总览

| 方式 | 适用 | 操作 |
|---|---|---|
| **A. GHCR 镜像仓库拉取**（推荐，迭代最快） | 有 GitHub 账号，镜像想随时更新 | 见 §1「GitHub Container Registry」 |
| **B. 飞牛导入镜像**（一锤子） | 已在别处构建好镜像 tar | 见 §2 |
| **C. 飞牛/主机源码构建** | 只有一台有 Docker 的机器 | 见 §3 |

---

## 1. 方式 A：GHCR 镜像仓库（飞牛拉取迭代）

> 镜像存 GitHub Container Registry（ghcr.io），飞牛 `docker pull` 即可运行，每次更新 = 推新镜像 → 飞牛重新拉取。

### 1.1 创建 GitHub 仓库（一次性）

1. 登录 [github.com](https://github.com)，点右上角 **+ → New repository**；
2. Repository name 填如 `yuqing-analysis`；**Public**（公开，飞牛无需登录即可拉取）；勾选 *Add a README*；点 **Create repository**。

### 1.2 生成访问令牌 PAT（一次性）

1. 右上角头像 → **Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate new token**；
2. 勾选权限：**`write:packages`**（推镜像必需；建议同时勾 `read:packages`、`repo`）；
3. 生成后**立即复制**令牌（只显示一次），妥善保存。

### 1.3 构建并推送镜像（每次发版执行）

在任意有 Docker 的机器（本机 / 飞牛 / VPS）上：

```bash
# 1) 登录 GHCR（把 <用户> 换成 GitHub 用户名，粘贴刚才的令牌）
echo "<你的PAT令牌>" | docker login ghcr.io -u <你的GitHub用户名> --password-stdin

# 2) 构建（镜像名必须是 ghcr.io/用户名/仓库名 的格式）
cd 项目目录
docker build -t ghcr.io/<你的GitHub用户名>/yuqing-analysis:latest .

# 3) 推送
docker push ghcr.io/<你的GitHub用户名>/yuqing-analysis:latest
```

### 1.4 飞牛拉取并运行

**命令行方式**（飞牛终端/SSH）：
```bash
docker pull ghcr.io/<你的GitHub用户名>/yuqing-analysis:latest
docker run -d --name yuqing -p 5000:5000 \
  -v /vol1/docker/yuqing/data:/app/data \
  -e TZ=Asia/Shanghai \
  --restart unless-stopped \
  ghcr.io/<你的GitHub用户名>/yuqing-analysis:latest
```

**飞牛 Docker 界面方式**：
1. 飞牛 **Docker → 镜像 → 拉取**，输入 `ghcr.io/<你的GitHub用户名>/yuqing-analysis:latest`；
2. 拉取成功后在镜像列表点**创建容器**：端口映射 `5000→5000`；存储挂载 `/app/data` 到飞牛本地目录；环境变量 `TZ=Asia/Shanghai`；
3. 启动容器，浏览器访问 `http://<飞牛IP>:5000/`。

### 1.5 日常迭代（改代码 → 飞牛更新）

```bash
# 构建机：
docker build -t ghcr.io/<用户>/yuqing-analysis:latest .
docker push ghcr.io/<用户>/yuqing-analysis:latest

# 飞牛：
docker pull ghcr.io/<用户>/yuqing-analysis:latest
docker stop yuqing && docker rm yuqing
docker run -d --name yuqing -p 5000:5000 -v /vol1/docker/yuqing/data:/app/data -e TZ=Asia/Shanghai --restart unless-stopped ghcr.io/<用户>/yuqing-analysis:latest
```
数据卷 `/vol1/docker/yuqing/data` 保留，历史报告不丢。
建议再加个 tag 版本：`docker tag … :v0.2 && docker push … :v0.2`，飞牛可回滚。

### 1.6 注意

- 镜像内不含 `.env` / 密钥（`.dockerignore` 已排除）；AI Key 用**挂载 .env** 或容器环境变量注入；
- ghcr.io 国内访问偶有波动：拉不动时可加容器镜像加速，或退回方式 B（tar 导入）；
- Public 仓库任何人都能拉镜像（不含敏感数据），介意可建 Private（拉取需用 PAT 登录，见 §1.2）。

## 2. 方式 B：飞牛导入镜像（tar 导入，一锤子）

### 2.1 在构建机（任意有 Docker 的主机）构建并导出

```bash
# 在项目根目录（含 Dockerfile）执行
docker build -t yuqing:latest .
# 导出为可导入的 tar（gzip 压缩）
docker save yuqing:latest | gzip > yuqing-docker.tar.gz
```

### 2.2 飞牛导入镜像

1. 打开飞牛 **Docker → 镜像**；
2. 点击**导入**，选择 `yuqing-docker.tar.gz`；
3. 导入成功后，**创建容器**：
   - 镜像：`yuqing:latest`
   - 端口映射：`5000 → 5000`（外部端口可改，如 `8080`）
   - 存储挂载：`/app/data` → 飞牛本地目录（如 `vol1/docker/yuqing/data`），**报告与历史都会存在这里**
   - 环境变量：`TZ=Asia/Shanghai`（默认已带）
4. 启动容器，浏览器访问 `http://<飞牛IP>:<映射端口>/`。

### 2.3 配置 AI 与信源（部署后配置）

容器默认带 `.env`（AI Key 为空可先跑采集，AI 分析会提示未配置）。配置有两种：
- **推荐（挂载 .env）**：在飞牛创建容器时，把主机上的 `.env` 文件挂载为 `/app/.env`（只读）——参考 `docker-compose.yml` 的 `./.env:/app/.env:ro`；
- 或进入容器终端编辑 `/app/.env` 后重启容器。

`.env` 关键项：
```ini
SEARXNG_URL=https://searxng.6556888.xyz   # 或内网 http://192.168.68.112:8889
DEEPSEEK_API_KEY=sk-...                    # 或 QWEN_API_KEY
AI_ROUTER_BASE_URL=...                     # 本地 9router（可选）
AI_PRIMARY_PROVIDER=                       # 留空自动选择
```

---

## 3. 方式 C：源码构建（飞牛 / 任意 Linux）

把整个项目目录（含 `Dockerfile`、`backend/`、`frontend/`）上传到主机任一目录：

```bash
cd 项目目录
docker build -t yuqing:latest .
docker run -d --name yuqing -p 5000:5000 \
  -v "$PWD/data:/app/data" \
  -v "$PWD/.env:/app/.env:ro" \
  --restart unless-stopped \
  yuqing:latest
```

或使用 compose：

```bash
cp .env.example .env   # 填写 AI Key 后
docker compose up -d
```

---

## 4. 升级 / 迭代流程

因本项目仍处迭代期，升级遵循"小步快跑"：

```bash
# 拉取最新代码后，重新构建并替换容器
docker build -t yuqing:latest .
docker compose up -d --force-recreate
# 数据卷（./data）保留，历史报告不丢
```

飞牛导入方式：重新 build → save → 导入新版本镜像 → 删除旧容器（保留数据卷）→ 用新镜像创建容器并挂载同一数据目录。

---

## 5. 常见问题

| 现象 | 处理 |
|---|---|
| 首页打不开 / 端口不通 | 检查容器是否 Running；宿主防火墙放行映射端口 |
| ghcr.io 拉取超时/失败 | 国内访问波动：加容器镜像加速（Registry Mirrors），或改 §2 tar 导入 |
| 生不成 docx | 容器内 `node --version` 应 ≥18；`backend/scripts/node_modules` 存在（见 Dockerfile 构建日志） |
| AI 提示未配置 | `.env` 未生效：检查挂载路径或重启容器 |
| 集采无结果 | SEARXNG_URL 是否可达；信源组在页面/配置中是否勾选 |
| 时区错乱 | 设置 `TZ=Asia/Shanghai` 环境变量 |

---

## 6. 镜像构成说明（便于排查）

```
/app
├── backend/            # Python 后端（flask + requests + dotenv，纯 Python）
│   ├── app.py          # 入口：0.0.0.0:5000
│   ├── scripts/
│   │   ├── gen_docx.mjs      # Node docx 生成
│   │   └── node_modules/     # 来自构建期 node-stage
│   └── docker_entrypoint.sh  # 首次初始化 .env 后启动
├── frontend/           # 静态页面
├── data/               # VOLUME：tasks/ reports/ raw/
└── .env                # 默认模板；生产用挂载覆盖
```
