# 舆情分析平台（miaoyu-analysis）

多信源舆情分析系统：**SearXNG 抓取 → AI 四段式分析（事件概况/原因/风险/对策）→ Word/Markdown/PDF 报告**。
面向"部署后任何人开箱即用"：管理员在页面/`.env` 配置 AI 与信源即可，支持 Docker 一键部署与飞牛 fnOS 安装。

- 报告格式严格对齐《参考格式.docx》四段式模板（标题→引言→一、事件概况→二、原因→三、风险→四、对策，分论点"（一）（二）"句首加粗）
- 每段行文标注来源媒体；事实仅取自权威媒体，否则明确标注局限
- **对策与风险一一对应**（id 硬校验 + 顺序回填），严禁凭空编造
- 导出：Word（docx）/ Markdown / PDF（浏览器打印）

相关文档：[部署指南 docs/DEPLOY.md](docs/DEPLOY.md) ｜ [开发手册与任务清单 docs/开发手册.md](docs/开发手册.md) ｜ [开源项目调研与演进路线 docs/开源项目调研与路线图.md](docs/开源项目调研与路线图.md)

---

## 快速开始

### 本机（开发）

```bash
cd backend
pip install -r requirements.txt        # 纯 Python 依赖
cd scripts && npm install               # docx 生成（Node）
cd ../.. && cp .env.example .env        # 填写 AI / SearXNG
python backend/app.py --port 5000       # 浏览器 http://localhost:5000
```

### Docker / 飞牛

```bash
docker pull ghcr.io/ruoshui6662/miaoyu-analysis:latest
# 详见 docs/DEPLOY.md（compose 配置与数据卷）
```

代码推送到 `main` 分支后，GitHub Actions 会自动重建镜像，飞牛 `docker compose pull && docker compose up -d` 即可升级。

---

## 配置（.env）

| 配置 | 说明 |
|---|---|
| `SEARXNG_URL` | SearXNG 实例（公网或内网） |
| `AI_ROUTER_BASE_URL` / `AI_ROUTER_API_KEY` / `AI_ROUTER_MODEL` | 本地 LLM 网关（OpenAI 兼容，如 9router） |
| `DEEPSEEK_API_KEY` / `QWEN_API_KEY` | 官方直连（备选 provider） |
| `AI_PRIMARY_PROVIDER` | 强制指定 router / deepseek / qwen（留空自动） |

---

## 性能与稳定性：已踩过的坑与对策（重要，复现问题先看这里）

> 2026-08-31 实测沉淀。任何"慢 / 卡住 / 章节缺失 / 空报告"问题，按此排查，**不要推翻以下结论**。

### 1. 模型选型是最大的速度杠杆（最常见根因）

同一 9router 链路上实测生成速率：

| 模型 | 速率 |
|---|---|
| `ali/deepseek-v4-flash-0731` | ≈68 字/s |
| `ali/qwen3.7-flash` | ≈18 字/s（慢 3.8 倍） |

单章输出 1500-2500 tokens 时，慢模型可把"风险分析"从 1 分钟拖到 9 分钟。
**对策：默认用最快且质量达标的 flash 模型；切换模型前先跑基准**（`python -c` 小请求 + 中生成测速率）。

### 2. 并发是负优化：触发 burst 限流（429）

后端模型有突发限流：3 路并发实测直接 429（Throttling.BurstRate），退避重试连锁使总耗时 **220s > 串行 83s**。
**对策：AI 四章串行执行（事实→原因→风险→对策），克制并发；429 自动退避重试（2s/5s/10s）只是兜底。**

### 3. 流式在本链路更慢且偶发空响应

实测流式每章慢 2-3 倍，且部分网关对 `json+stream` 组合返回空。
**对策：默认非流式**（`ai_client.chat` 走非流式；`chat_stream` 保留供需要时用）。

### 4. 空内容响应（200 但 content 为空）——"章节丢失/关键词降级"的元凶

长 prompt 请求在网关侧偶发/频发返回空 content。此前表现为：事实整理两档失败 → 事件概况缺失；关键词扩展失败 → 退回规则关键词。
**对策：`_chat_nonstream` 空响应自动退避重试（3s/6s）再报错；`_facts_section` 支持素材减半降档重试。**

### 5. 对策-风险对应：id 硬校验 + 顺序回填

模型可能漏填 `for_id`（实测整批为空），硬校验会把这些对策全部剔除。
**对策：`_validate_risk_advice`：先按显式 id 匹配；漏填时按"对策输出顺序 ↔ 风险清单顺序"回填（保留告警），仍剩的风险在报告/日志中提示遗漏。严禁无源对应。**

### 6. 护栏

- 单请求 read 超时 300s（`timeout=(10, 300)`）；
- 单章失败降级为占位并写明原因，不阻塞后续章节；
- 每章失败原因通过进度回调透出到前端（不再"无声卡住"）。

### 7. 推理模型 `reasoning_tokens` 吃预算 —— 空内容/超时的隐藏根因（2026-09-01）

混合推理模型（如 `ali/deepseek-v4-flash-0731`）的 `reasoning_tokens` **计入 `max_tokens` 预算**：小预算请求全被推理吃光 → 返回 200+空内容（被误判成"通道故障"）；大预算也侵蚀正文额度。
**对策：请求统一带 `enable_thinking: false`**（顶层参数，OpenAI 兼容网关识别；不识别的会忽略）。实测同一事实整理请求：开推理 260s+ 失败 → 关推理 **13s 成功**。预检冒烟 `max_tokens` 别再给 8/32 这类小值（会误报）。

### 8. AI 通道容灾：双层故障转移（2026-09-01，借鉴 LiteLLM router）

9router→上游 502（`fetch connect timeout`）是**通道级**故障：换模型没用，得换通道。
**对策：服务商链 × 每服务商模型候选表（`fallback_models`，落 settings.db）双层转移**；鉴权错误冷却 60 分钟、抖动错误连续失败 2 次才冷却 5 分钟；任务开始前**预检冒烟，全通道不可用就快速失败**，不拖采集跑完产出四个空章节。详见开发手册 §5-11。

### 实测预期

- 小请求（1 token）：通道空闲 ≈1s，繁忙 ≈5s-10s（>10s 说明网关/后端繁忙）；
- 单章生成：15s-2min（取决于模型与输出量）；
- 完整一次分析：**3-5 分钟是正常水平**（采集 30-60s + AI 四章串行）。

### 排查流程

1. 看日志中各章节耗时（`[step] ai` 行间隔）；
2. 用小请求测通道底噪（>10s 说明网关忙，非本项目问题）；
3. 用模型基准对比测速率（见本文 §1）；
4. 检查容器 `.env` 是否生效（`docker exec <容器> cat /app/.env`，注意 compose `.env` 挂载需文件真实存在）。

---

## 目录结构

```
backend/            Flask 后端（纯 Python，无 C 扩展依赖）
  ├─ app.py          Web 入口（0.0.0.0:5000）
  ├─ collector.py    SearXNG 多信源采集/清洗/可信度/媒体名
  ├─ ai_client.py    OpenAI 兼容多后端（router/deepseek/qwen）+ 429/空响应重试
  ├─ ai_prompts.py   四角色 Prompt（媒体标注/权威约束/风险 id/对策 for_id）
  ├─ pipeline.py     编排：关键词→采集→四章分析→对策校验→docx/md
  └─ scripts/        Node docx 生成
frontend/           单页前端（无构建，本地资源）
docker-compose.*.yml Docker 部署
docs/               部署指南 + 开发手册（任务状态清单）
```

## 路线图（详见 docs/开发手册.md）

先完成已开发项的人工验证与工程收口（G0）→ 统一证据模型/来源适配器（G1）→ 持续监测与去重告警（G2）→ 安全、可观测性与交付（G3）→ 多场景与分析增强（G4/F）。详见 `docs/开源项目调研与路线图.md`。
