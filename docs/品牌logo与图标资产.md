# 品牌 Logo 与功能图标资产规则

## 结论

妙舆的图标分成两层，不能混用：

1. **功能图标**：导航、展开、删除、分类等界面动作，继续使用项目内的 24×24 单色线性 SVG，统一 `stroke-width: 1.8`、圆角端点和现有设计 token。
2. **品牌 Logo**：仅用于识别来源平台，使用 Simple Icons 已收录的官方品牌 SVG；保持原始几何比例，在来源行统一放入 27×27 图标槽位，颜色由界面 token 控制。

## 本次接入

`frontend/assets/source-logos.svg` 固化了 Simple Icons v16.21.0 的品牌符号，并通过本地 SVG sprite 使用，不依赖运行时 CDN。来源解析器采用域名优先、无域名来源才允许名称匹配的规则；未固化来源不再在运行时请求远程 favicon，而是显示统一的中性“待匹配”标记：

| 来源 | Logo 标识 | 备注 |
|---|---|---|
| 微博 / 微博热搜 | `sinaweibo` | 已覆盖 |
| 知乎 / 知乎热榜 | `zhihu` | 已覆盖 |
| B站 / B站热门 | `bilibili` | 已覆盖 |
| 小红书 | `xiaohongshu` | 已覆盖 |
| 百度百家号 / 百度热搜 | `baidu` | 已覆盖 |
| 微信公众号 | `wechat` | 已覆盖 |
| 豆瓣 | `douban` | 已覆盖 |
| V2EX | `v2ex` | 已覆盖 |
| 抖音热点 | `tiktok` | 使用同一品牌体系的官方图形；后续如纳入独立 Douyin 资产，替换映射即可 |

没有经过可靠品牌资产核验的来源不制作仿制 Logo，也不使用分类图标冒充来源身份。这样能避免错误标识、错位和跨来源串牌，同时保留后续逐个补齐的入口。

## 本地 Logo 库构建任务

已建立 `frontend/assets/source-logos/library/`，并提供 `tools/fetch-logo-library.ps1` 作为可重复执行的构建任务。它逐条读取 `/api/sources`，按“Simple Icons → 官网图标入口 → NAS HD-Icons 高置信候选”的顺序抓取，使用内容签名排除 HTML、登录页和错误页，最终生成每个来源的本地文件与 `manifest.json`。

前端启动时读取 manifest：已有核验品牌 sprite 优先；manifest 中 `status=fetched` 的本地文件其次；其余来源统一显示中性待匹配标记。`pending` 来源不会因为模糊匹配、分类图标或远程 favicon 而强行显示相似 Logo。这样把“识别准确性”和“部署稳定性”拆开处理：Logo 可以逐步补齐，运行时不会因为远程站点变更出现破图或串牌。

构建命令：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\fetch-logo-library.ps1
```

生成结果以 `frontend/assets/source-logos/library/manifest.json` 为唯一审计入口；其中官网和 NAS 资产默认标记为需人工复核，不自动宣称官方或商用授权。

## 2026-09-04 本次构建结果

当前信源目录共处理 83 条：44 条已固化到本地，39 条保持 pending。其中 15 条来自 Simple Icons，29 条来自对应官网图标入口，NAS 当前不再自动提供政府机构 Logo；另 1 条官网/NAS 候选在复核时因名称过于通用被拒绝（新华社误命中 `news-now`，已删除）。公安部使用官方政务平台 favicon，并固定为官方 URL。

这说明“官网可访问”与“能得到正确品牌 Logo”不是同一个条件：政府站点、聚合源和部分媒体站点可能返回登录页、HTML 或通用图标。pending 是有意保留的安全状态，后续应通过人工确认官方 SVG、品牌媒体包或站点明确的图标地址后再入库。

## 匹配顺序

1. 本地品牌资产：有官网域名的来源只能按域名匹配，解决同一平台的别名并阻止跨来源串牌。
2. 无官网域名的热榜来源才按清洗后的完整名称精确匹配；不再对有域名来源做模糊匹配。
3. 官网图标：读取信源目录中的第一个官网域名，依次尝试 `https://域名/favicon.ico` 和 `https://域名/apple-touch-icon.png`；图片失败不会显示破图，而是自动使用分类图标。
4. 分类图标：没有域名、没有可信品牌匹配或官网图标不可访问时的稳定兜底。

官网图标是构建阶段的候选来源，而非运行时展示层：它受目标站点可用性、反爬和站点改版影响，不能替代已核验的本地 Logo。后续高频来源应将官网图标人工复核后固化到本地资产。

## 许可与托管

- 图标源： [Simple Icons GitHub](https://github.com/simple-icons/simple-icons)，项目仓库标注为 CC0-1.0；每个品牌仍属于其各自权利人，产品只作来源识别，不表示任何品牌背书。
- 版本来源： [Simple Icons v16.21.0 CDN 文件规范](https://github.com/simple-icons/simple-icons/blob/develop/README.md#cdn-usage)。本项目不在生产运行时直接请求 CDN，而是将已选 SVG 固化在仓库内。
- 后续发布到 GitHub 时，`frontend/assets/source-logos.svg` 与 `frontend/assets/source-logos/library/` 会随应用版本一起发布；已核验品牌不受第三方 CDN 波动影响，未固化来源不会依赖运行时远程图标。
- 新增品牌时必须记录：来源 URL、版本、Logo slug、许可/品牌规范、是否需要明示归属；不要从 favicon 聚合站抓取不可控的 PNG。

## 补充图标库评估

- [Iconfont](https://www.iconfont.cn/)：适合作为中文网站 Logo 的人工候选库；入库前必须确认上传者、项目授权和是否为官方品牌资产。
- [Flaticon](https://www.flaticon.com/)：可提供 SVG，但免费资源通常要求署名，部分带商标的资源可能只允许编辑用途，不作为默认品牌资产源。
- [Icons8](https://igoutu.cn/icons)：风格统一，适合功能图标和分类兜底；免费使用需要按其许可添加链接，不直接并入无署名 Logo 主库。
- [The Noun Project](https://thenounproject.com/)：适合语义图标和成套分类图标；单个资源许可和署名条件需要逐项核对，不自动抓取到生产资产。

这些站点均只作为人工选材参考，不作为运行时 Logo API。生产展示优先使用已核验并固化到本地的 SVG；未固化来源使用统一待匹配标记，待人工确认后再固化资产。

## 展示规则

- Logo 只承担“这条来源是谁”的识别，不承担启用状态、可信等级或热度含义；状态仍由开关和 S/A/B/C/D 徽标表达。
- 首页六个平台热榜使用独立的稳定映射：`source_id → frontend/assets/source-logos/library/*`，不再依赖热榜名称、首字母或热度颜色生成标识；同一平台在焦点、刚刚发生和当前热榜区域使用同一资产。
- 尺寸固定为 17px 视觉图形 / 27px 槽位，桌面与移动端不改变比例；品牌图形不拉伸、不裁剪、不加阴影。
- 装饰性 Logo 使用 `aria-hidden="true"`，来源名称仍作为可访问文本；独立 Logo 按钮必须额外提供可访问名称。
- 没有品牌 Logo 的来源不显示文字首字母、分类图标或远程 favicon，使用统一的中性待匹配标记，避免错误识别和视觉漂移。
