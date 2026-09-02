# API 接入前置与多源轮询方案

> 版本 v0.1 ｜ 调研日期：2026-09-02 ｜ 状态：前置条件已整理，等待密钥与接入排期

## 1. 第一性原理

舆情采集不是“找到一个万能新闻 API”，而是把不同来源的观测统一成可追溯的 `Mention`：

```text
来源 API / RSS / 搜索实例
        ↓
来源适配器（鉴权、限流、分页、重试）
        ↓
统一 Mention（标题、摘要、原文 URL、发布时间、来源、来源等级）
        ↓
去重、时间窗过滤、来源健康度
        ↓
事实证据 / 舆论样本 / 趋势统计 / AI 分析
```

因此，“多个 API 轮询”必须拆成两种机制：

1. **异构来源并行汇聚**：GDELT、RSS、SearXNG、YouTube 等各自贡献不同来源，不互相伪装成同一数据；
2. **同类服务的 Key/Endpoint 轮询**：同一 API 的多个 Key 或多个实例按 round-robin、权重和冷却状态切换，用于配额和故障转移。

不能仅仅把多个 URL 依次请求后拼接结果，否则会重复计数、混淆可信度，也无法解释某条信息为何进入报告。

## 2. 当前项目的已知基础

当前代码已经具备：

- SearXNG JSON 搜索主链路；
- `hotlists.py` 的热榜/JSON Feed 适配雏形；
- SQLite 信源目录和启用开关；
- AI 服务商注册表、重试、冷却和 provider fallback；
- `collector.py` 的标题、摘要、URL、域名、媒体名和可信度处理。

当前缺口：

- 公开 API 尚未统一成来源适配器契约；
- 没有 `Mention` 唯一键、跨源去重和来源健康表；
- 没有趋势时间序列，无法直接填充效果图中的变化率/折线图；
- 没有证据记录模型，无法直接填充“关键证据”和“可信度”；
- NewsNow 中国热榜公开主链路已接入首页；API Key 仍只用于 AI 与未来的 TopHubData/增强源，不能混同为热榜授权。

## 3. 免费优先的 API 清单

### 3.1 第一批：无需注册即可开始

| 来源 | 类型 | 前置条件 | 适合用途 | 限制与结论 |
|---|---|---|---|---|
| GDELT DOC 2.0 / Context 2.0 | 全球新闻检索/上下文 | 无 Key；服务端 HTTP 请求 | 全球新闻、时间窗、主题扩散、上下文句子 | 无稳定 SLA，需自限速、缓存和失败降级；建议作为海外新闻基础源 |
| RSS/Atom | 站点公开订阅 | 无 Key；需要维护 feed URL | 政府官网、媒体官网、行业站点的持续轮询 | 各站字段和更新频率不同；必须保留原始 URL 与抓取时间 |
| Hacker News API | 官方公开只读 JSON | 无 Key、无需登录 | 海外科技/开发者社区舆论样本 | 来源偏技术社区，不能代表大众舆情；官方文档称当前无速率限制，但仍应礼貌限速 |
| 自建 SearXNG | 聚合搜索 API | 本机或服务器部署；开启 JSON 输出 | 中文新闻、网页、站点定向搜索、正文入口发现 | 公共实例的 JSON 可能被关闭；生产主路径应自建，不依赖公共实例 |

调研时的只读连通性验证：GDELT DOC 返回 HTTP 200；Hacker News 返回 500 条 top stories；当前 SearXNG 返回 JSON 搜索结果。GDELT 在 PowerShell 中曾出现本机 TLS 信任链错误，但 Python `requests` 请求成功，属于本机请求栈差异，不应据此判断 API 不可用。

### 3.2 第二批：需要注册，但免费额度可用于开发

| 来源 | 注册/登录 | 当前公开条件 | 适合用途 | 是否建议现在接入 |
|---|---|---|---|---|
| The Guardian Open Platform | 注册 Developer API Key | 非商业用途免费；最多 500 次/日、约 1 req/s，可取文章正文 | 英文权威新闻与正文 | 建议。若产品商业化，需申请 Commercial key；Developer key 不能直接当生产授权 |
| YouTube Data API v3 | Google 账号 + Cloud Project + 启用 API + API Key | 默认配额 10,000 units/day；搜索接口当前单独受每日调用配额约束 | 视频标题、频道、发布时间、视频舆论样本 | 可选。搜索配额消耗和政策要求较严格，不作为第一主链路 |
| NewsAPI Developer | 注册账号并获取 Key | 免费开发计划 100 requests/day、文章延迟约 24 小时、仅开发/测试环境 | 联调新闻检索、前端演示 | 仅用于开发联调；不能用于 staging/production 或内部生产使用 |
| DeepSeek / 通义千问 | 各自平台注册并充值/获得额度 | 当前项目报告生成必须有至少一个有效 AI Key | 四段式报告生成、结构化摘要 | 需要用户准备至少一个 Key；AI Key 不应提交 Git |
| TopHubData | 控制台注册/购买或获得 Key | 当前项目已预留 `TOPHUBDATA_KEY`；无 Key 时走 HTML 兜底 | 热榜官方接口 | 暂不作为免费主链路，继续保留 HTML 降级 |

### 3.2.1 本轮候选站点实测

| 站点 | 只读可达性 | 可用方式 | 结论 |
|---|---|---|---|
| [NewsNow](https://newsnow.busiyi.world/) | 公开实例首页 200；`GET /api/s?id=...&latest=true` 可返回 JSON | 已验证 `weibo`、`zhihu`、`bilibili`、`douyin`、`baidu`、`toutiao`、`hackernews`、`v2ex` 等 ID；响应含 `status`、`updatedTime`、`items[]` | **首选无 Key 适配器**。但这是第三方公共实例，数据有缓存、可能限流或停止；生产建议自建 [ourongxing/newsnow](https://github.com/ourongxing/newsnow) |
| [REBANG](https://rebang.open2hub.com/) | 301 跳转至 `top.open2hub.com`，HTML 200；`robots.txt` 当前允许 `/` | 服务端 HTML 中可见抖音、微博、百度、快手等榜单条目 | 可作为**第二 HTML 兜底源**，不把页面结构当稳定 API；优先抓公开榜单文本和原文链接 |
| [今日热榜](https://tophub.today/c/tech) | 分类页 HTML 200；`robots.txt` 未提供可用规则文件 | 公开 HTML 榜单；当前项目 `hotlists.py` 已有 tophub HTML 兜底 | **继续复用现有适配器**，不要重复造一套抓取链路 |
| [SoPilot](https://sopilot.net/zh/hot-tweets) | 页面 HTML 200，内容主要是 X 热帖 | 页面用于 X 热帖监控；`robots.txt` 明确禁止 `/api` 及 `/zh/api` | 不接入 API；只可把公开页面视为人工参考，不绕过其 API 禁止规则，也不自动化 X 互动 |
| [英为财情](https://www.investing.com/) | 页面可见，但本次程序请求首页返回 403；站点声明数据未必实时/准确 | 财经行情与资讯页面，不是中国内地热榜 API | 不纳入舆情热榜；若未来做行情模块，应寻找正式授权数据 API |
| [萝卜投研链接](https://luobo.cn/) | 200，但实际内容是《保卫萝卜》游戏官网 | 不是萝卜投研 | **链接误配**。萝卜投研属于通联数据/DataYes，相关专业服务需要单独确认授权和账号，不作为免费抓取前置 |

NewsNow 上游仓库公开说明了 `/api/s` 类数据接口、自建方式、默认缓存和自适应抓取间隔；公共实例只适合联调/个人演示，不能把它当成永久稳定的第三方 SLA。当前实测结果仅代表 2026-09-02 的可达状态。

### 3.3 暂不作为免费基础依赖

- X、微博、知乎、抖音、小红书等平台原生接口：通常没有稳定、开放、免费且适合批量舆情检索的匿名 API；登录态抓取还涉及账号安全、平台条款和反爬风险。NewsNow 是对其中部分公开榜单的第三方聚合，不等于取得了平台原生授权。
- B 站公开接口：可以作为热榜的可选交叉校验，但部分接口未形成稳定的公开开发者契约，不能承担主链路。
- 各类“万能热榜 API”聚合站：需要逐一核对授权、数据来源、商业使用范围和 SLA，不能仅凭“免费”接入报告事实链。

## 4. 用户需要准备的前置条件

### 不需要用户注册

首轮可以直接使用：GDELT、Hacker News、RSS，以及本机自建 SearXNG。SearXNG 需要安装并配置实例，而不是第三方账号登录。

### 需要用户注册/登录

1. **AI 至少一个**：DeepSeek 或通义千问，任选其一即可启动完整分析；两个都准备则可实现 AI provider fallback。
2. **Google Cloud + YouTube API Key**：仅在需要视频信源时准备。
3. **Guardian Developer Key**：仅在需要英文权威新闻正文时准备；非商业开发可用。
4. **NewsAPI Key**：不是必须，仅用于开发联调，不建议作为生产依赖。
5. **TopHubData Key**：不是必须，当前无 Key 仍可使用 HTML 兜底；如需要官方热榜接口再准备。

密钥交付方式：只填本机 `.env` 或设置页，不要在聊天中发送，不要写入 Git、截图、报告原文或前端响应。当前项目的 `.env` 已被 Git 忽略，`.env.example` 只放空值模板。

## 5. 统一适配器契约

后续每个来源实现同一组最小方法：

```python
class SourceAdapter:
    id: str
    capabilities: set[str]  # search / feed / trend / article / video

    def search(self, query: str, *, since: str, until: str | None,
               limit: int) -> list[dict]: ...

    def health(self) -> dict: ...
```

统一输出至少包含：

```json
{
  "source_id": "gdelt",
  "source_name": "GDELT DOC",
  "source_type": "news_search",
  "source_level": "A",
  "title": "...",
  "summary": "...",
  "url": "https://...",
  "published_at": "2026-09-02T09:30:00Z",
  "captured_at": "2026-09-02T09:31:12Z",
  "language": "en",
  "external_id": "...",
  "raw_ref": "data/raw/..."
}
```

`Mention` 唯一键建议按以下顺序生成：`source_id + external_id`；没有外部 ID 时使用规范化 URL；URL 缺失时再使用标题、发布时间和来源的哈希。不能只按标题去重，否则同题不同报道会被误删。

## 6. 多 API 轮询规则

### 6.1 调度顺序

```text
读取启用来源
  ↓
按能力选择（search/feed/trend/article/video）
  ↓
同一能力内按 priority + cooldown 排序
  ↓
并行请求不同来源；同一来源的多个 Key 顺序轮询
  ↓
标准化 → 去重 → 可信度标注 → 缓存
  ↓
返回成功结果与 source_health，不因单源失败中断整次分析
```

### 6.2 失败处理

| 状态 | 动作 |
|---|---|
| 200 | 记录成功时间、延迟、条数；恢复连续失败计数 |
| 401/403 | 当前 Key 标记 `invalid`，暂停该 Key，提示设置页检查；不盲目重试 |
| 429 | 按 `Retry-After` 或指数退避冷却当前 Key/实例，不影响其他来源 |
| 5xx/网络超时 | 最多重试 1–2 次，再切换同类下一个 Key/Endpoint |
| 解析失败 | 记录 schema 错误并隔离该来源，不把异常数据送入 AI |
| 空结果 | 记录为成功但无命中，不能当作来源故障 |

### 6.3 配置形态（设计稿）

后续可用一个 JSON 注册表支持多个 Endpoint/Key：

```json
[
  {
    "id": "gdelt-main",
    "adapter": "gdelt_doc",
    "enabled": true,
    "priority": 10,
    "endpoint": "https://api.gdeltproject.org/api/v2/doc/doc",
    "api_key_env": "",
    "rate_limit_per_minute": 10,
    "cooldown_seconds": 300
  },
  {
    "id": "guardian-dev",
    "adapter": "guardian",
    "enabled": false,
    "priority": 20,
    "endpoint": "https://content.guardianapis.com",
    "api_key_env": "GUARDIAN_API_KEY",
    "rate_limit_per_second": 1,
    "daily_limit": 500,
    "cooldown_seconds": 900
  }
]
```

Key 只保存环境变量名，不把明文 Key 写进注册表。后续设置页可展示“已配置/未配置、今日用量、冷却状态、最近成功、最近错误”，但不能回显密钥。

## 7. 分阶段前置开发规划

| 阶段 | 目标 | 交付物 | 用户是否需要操作 |
|---|---|---|---|
| API-0 | 中国热榜无 Key 基础链路 | ✅ 已完成 NewsNow（微博/知乎/B站/抖音/百度/头条）主源、5 分钟缓存、单榜健康隔离；保留 TopHub/REBANG 适配位 | 不需要注册；接受公共聚合站缓存与失效风险 |
| API-1 | 中国热榜公开源接入 | ✅ 已完成首页 JSON 接入与 provider/source_health；TopHub/REBANG HTML 在 NewsNow 缺榜时降级；统一 Mention 入库仍归 G1 | 不需要 |
| API-2 | 多实例/多 Key 轮询 | provider 注册表、round-robin、429 冷却、健康检查、用量统计 | 需要提供想接入的 Key |
| API-3 | 效果图数据能力 | trend、evidence、confidence、source_count 等字段填充首页 | 需要确认信源范围与时间窗 |
| API-4 | 增强源 | Guardian、YouTube、NewsAPI 可选适配器 | 按需注册对应服务 |

## 8. 本轮结论

- 不需要等待注册即可继续做 API-0/API-1；
- 当前已完成首轮免费接入：本地 `/api/hot/boards` 实测返回 6 个榜单、89 条条目；`paid_apis_enabled=false`。
- 可调整的公开源配置：`NEWSNOW_ENABLED=false` 可整体切换，`NEWSNOW_BASE_URL` 可切换到自建 NewsNow，`NEWSNOW_CACHE_SECONDS` 最小 60 秒；公共实例建议低频使用。
- 付费接口暂不接入：`PAID_APIS_ENABLED` 默认 `false`，即使存在 `TOPHUBDATA_KEY` 也不会发起付费 TopHubData 请求；后续 API-2 再做多 Key/Endpoint 轮询。
- 最优先的用户前置是：准备至少一个 AI Key，并确认是否允许本机部署 SearXNG；
- Guardian 是最有价值的免费增强源，但免费 Developer key 只适用于非商业用途；
- YouTube 适合补视频，不适合作为全网舆情主搜索；
- NewsAPI 只用于开发联调，不能作为生产主链路；
- 微博、知乎、抖音、小红书暂不要求用户提供登录态，也不把模拟登录作为前置方案。

## 9. 官方资料

- [SearXNG Search API](https://docs.searxng.org/dev/search_api.html)
- [GDELT DOC 2.0 API](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/)
- [Hacker News Official API](https://github.com/HackerNews/API)
- [The Guardian Open Platform](https://open-platform.theguardian.com/access/)
- [YouTube Data API Getting Started](https://developers.google.com/youtube/v3/getting-started)
- [YouTube Search: list](https://developers.google.com/youtube/v3/docs/search/list)
- [NewsAPI Pricing](https://newsapi.org/pricing)
