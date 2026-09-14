# RADAR-SRC-4 安全与稳定性设计

**状态：** 待用户审阅  
**日期：** 2026-09-14  
**范围：** 已存在的 RSS/Atom 端点采集链路；不增加网站深抓、账号登录、外部 Provider 或 AI 调用。

## 1. 目标与验收边界

本阶段把现有“单机可用”的雷达 Feed 抓取路径收紧为可在多个本地进程竞争同一 SQLite 数据库时安全运行的路径，并让每一个网络跳转和错误输出都保持受控。

本阶段完成后可以标记为“已开发待现场验收（🟡）”，但不能标记 RADAR-SRC-4 为 ✅✅。后者仍需要部署环境中的多实例观察、管理员配置的真实业务 Feed 端到端验收，以及连续 24 小时运行抽查。

验收目标：

- 同一端点同时被两个独立 Python 进程领取时，恰有一个进程得到租约；租约过期后另一个进程可以恢复采集。
- 采集租约在抓取前后持续有效；崩溃留下的过期租约不会永久阻塞，遗留 `running` 同步记录会以可审计方式结束。
- 自动请求逐跳检查重定向的协议、URL 凭据、DNS 解析地址和私网策略；不跟随超过固定上限的跳转。
- 私网来源默认拒绝。只有精确命中管理员环境变量白名单的主机名、IP 或 CIDR 才可访问；测试使用独立、临时的显式白名单，不能使用“全局关闭 SSRF”作为生产方案。
- Feed API、健康接口、运行记录和日志不返回或记录 `auth_ref`、URL 中的账号密码或响应正文。RSS/Atom 基线不接受任何 `auth_ref`，为将来的后端密钥引用保留数据库字段但不启用它。
- 自动化测试不访问互联网；24 小时项交付观测字段、稳定性演练脚本/清单和运行记录查询依据，不伪造 24 小时已运行事实。

## 2. 方案选择

### 方案 A：保留 SQLite，原子租约 + 有界抓取（采用）

使用现有 `source_fetch_states` 作为租约权威，保留 `BEGIN IMMEDIATE` 原子领取。新增续约与过期租约恢复，在每次端点采集结束时保证释放；恢复者会结束旧的未完成同步记录。RSS 请求改为手动、有上限的重定向循环，逐跳复用同一 URL/DNS 安全检查。

优点是无需引入 Redis、任务队列或新服务，适合目前单机 SQLite 部署；缺点是 SQLite 仍不是跨主机高并发任务系统，因此多容器现场验收仍是明确门槛。

### 方案 B：引入 Redis 分布式锁和队列（不采用）

可获得更成熟的多节点调度语义，但会改变部署拓扑、配置和故障模式；当前没有并发/规模证据，不符合单端口、轻量部署边界。

### 方案 C：仅扩大进程内锁（不采用）

不能协调多个进程或容器，也无法从崩溃恢复，直接不满足规范。

## 3. 组件与数据流

```text
RadarService
  → db.radar_endpoint_lease_acquire_or_recover(...)
  → radar_sources.fetch_feed(...)
       → validate URL + resolve/check address
       → GET(allow_redirects=False)
       → 3xx: validate Location and repeat (max 3)
       → stream with compressed-byte / timeout bounds
       → parse / normalize
  → atomically record state + sync run
  → db.radar_endpoint_lease_release(...)
```

### 3.1 租约与运行记录

- `lease_owner` 继续是每个 `RadarService` 实例生成的随机、无业务含义标识；绝不回传到前端。
- 租约持续时间定为 120 秒。当前连接/读取总超时上限为 25 秒，因此单次 RSS 拉取不会合法占满租约；每次采集完成后立即由持有者释放。
- 数据库领取函数在同一事务中识别 `lease_until <= now` 的过期租约，领取给新 owner，并将同端点遗留的 `running` 同步记录标记为 `abandoned`，错误码为 `lease_expired`，不写响应正文。
- 领取失败只是“本轮由其他 worker 处理”，不是端点失败：不增加失败计数、不推进游标、不进入退避。
- 现有单进程 `_run_lock` 仍只用于减少本实例重复工作，数据库租约是跨进程唯一权威。

### 3.2 SSRF 与重定向

- 只接受 `http`/`https`，拒绝 URL 用户名/密码、空主机和所有其它协议。
- 使用 `requests.get(..., allow_redirects=False)`；仅接受 301、302、303、307、308 的绝对或相对 `Location`，最多 3 跳。缺失或无效 Location 作为可解释的 Feed 错误。
- 每跳在连接前都调用 URL 检查和 DNS 解析复核。解析到回环、私网、链路本地、未指定、保留或多播地址即拒绝。
- 白名单由 `MIAOYU_RADAR_PRIVATE_SOURCE_ALLOWLIST` 提供，逗号分隔精确主机名、IP 或 CIDR。它只允许命中的目标，不能让任意私网请求通过；旧 `MIAOYU_RADAR_ALLOW_PRIVATE_SOURCES` 仅保留给旧测试兼容，生产文档不再把它作为配置方式。
- 保持现有 2 MiB 压缩响应上限和 `(5, 20)` 连接/读取超时；不保存响应正文。解压后内容大小无法由 `requests` 流层可靠获知，本阶段显式不承诺防御压缩炸弹；若生产出现该证据，再单列硬化任务。

### 3.3 凭据与脱敏

- RSS/Atom 创建、编辑和预览请求拒绝非空 `auth_ref`；不接受账号密码、Cookie、Authorization 输入或 URL 内嵌凭据。
- `db.radar_endpoint_get()` 可保留内部 `auth_ref` 字段以兼容既有架构，但所有 API 投影统一移除它；错误、健康状态、同步记录只记录最多 500 个字符的受控摘要。
- 不做“把明文 token 加密后继续使用”的伪安全设计；将来需要受保护 API 时，必须先接入现有后端密钥引用系统并另行评审。

## 4. 测试与观测

- `tests/test_radar_sources.py`：用两个真实子进程争抢同一临时 SQLite 文件；断言单获胜者、过期恢复与遗留运行记录状态。
- `tests/test_radar_sources.py`：固定 `requests` 响应或本地回环 HTTP 服务覆盖公网→私网重定向拒绝、跳转上限、每跳 DNS 重检、精确白名单、URL 凭据拒绝及无 `auth_ref` API 投影。
- 现有 200/304/503/cursor/退避测试继续是不可回退基线；新增测试确认租约竞争不增加失败计数。
- 健康页继续以 `last_checked_at`、`last_success_at`、`next_fetch_at`、`consecutive_failures`、`cooldown_until`、HTTP 状态和受控错误摘要作为 24 小时抽查证据。文档提供最少一端点、两实例、每 15 分钟的观察清单；该清单不是自动化“已运行 24 小时”的替代物。

## 5. 非目标与风险

- 不实现 RADAR-SRC-3 的网站 Feed 发现、RSSHub/Bridge 或公共账号 Adapter。
- 不启动默认后台 Playwright、深抓、账号登录、AI 或付费搜索调用。
- 不把 SQLite 宣称为跨区域分布式队列；发现锁争用、数据库损坏或扩缩容证据后再评估专用任务基础设施。
- 真实外部网络与 24 小时观察由部署环境执行；本仓库只提供可重复自动化覆盖和观测口径。
