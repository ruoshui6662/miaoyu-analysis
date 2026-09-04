# PDF 导出空白：根因排查与渲染方案

> 更新：2026-09-04 ｜ 状态：Playwright 链路已接入，待真实报告人工验收

## 1. 结论先行

当前空白 PDF 不是报告数据为空，也不是 PDF 阅读器兼容性问题，而是**浏览器端 `html2canvas → jsPDF` 截图式链路产出的 JPEG 位图本身就是纯白**。

报告在浏览器页面中能正常排版，说明 DOM、数据和页面 CSS 仍然有效；故障位于“把 DOM 光栅化为 canvas”的环节。因此继续调整捕获容器高度、位置、主题色或 ECharts 图表，不能从根上解决问题。

## 2. 第一性原理排查链

PDF 是否有内容，必须拆成四个可验证环节：

```text
报告 JSON 有数据 → reportBody 有可见 DOM → 渲染器生成非空位图/矢量内容 → PDF 页面包含非白色对象
```

本次逐层验证结果：

| 环节 | 证据 | 结论 |
|---|---|---|
| 报告数据 | 当前页面已显示标题、段落、三张图表、观点与附录 | 数据链路正常 |
| DOM 排版 | 临时克隆节点放回视口后，浏览器截图能看到完整报告 | DOM/CSS 正常 |
| 捕获尺寸 | 初始诊断发现 canvas 曾为 `703×0`；补充显式高度后仍失败 | 高度是旧问题，不是最终根因 |
| 位图内容 | 最新 PDF 内含 2 个 JPEG，尺寸为 `1600×2331`、`1600×1322`，两者 RGB 均值均为 `(255,255,255)`，极值也全为 255 | html2canvas 输出纯白 |
| 最小探针 | 仅加入黑色 `PDF_RENDER_PROBE` 文本，页面可见但导出 JPEG 仍全白 | 不是图表、复杂 DOM 或颜色 token 导致 |

尺寸问题已经被排除，当前 Chromium/页面环境下 html2canvas 的渲染结果不可用。它“成功返回 PDF”只代表流程没有抛异常，不代表画布有有效像素。

## 3. 为什么现有修复没有生效

现有实现试图解决三个常见问题：

1. 给捕获节点补 `width/height/min-height`，避免固定定位节点被测成 0 高度；
2. 在 `onclone` 中将节点改成 `position: static`、`visibility: visible`、`opacity: 1`；
3. 把 ECharts SVG 转为 canvas，避免 html2canvas 不支持 SVG 图表。

这些修复对“节点不可见”和“图表不可绘制”有效，但本次最小探针仍然是纯白，说明问题发生在更底层的截图式光栅化路径。继续堆叠 CSS hack 会增加浏览器、移动端和 Docker 部署差异，不能作为交付级方案。

## 4. GitHub 开源方案对比

| 方案 | 原理 | 复用当前 HTML/CSS | 难度 | 部署成本 | 判断 |
|---|---|---:|---:|---:|---|
| [Playwright](https://github.com/microsoft/playwright) | 无头 Chromium 真正执行页面，再调用 `page.pdf()` | 高 | 中 | 高：浏览器运行时、镜像体积、内存 | 首选 |
| [Puppeteer](https://github.com/puppeteer/puppeteer) | Chromium 页面打印为 PDF | 高 | 中 | 高：与 Playwright 同类 | 可替代首选 |
| [WeasyPrint](https://github.com/Kozea/WeasyPrint) | Python HTML/CSS 分页排版引擎 | 中 | 中 | 中：原生库、字体、CSS 差异 | 后端备选 |
| [Paged.js](https://github.com/pagedjs/pagedjs) | 浏览器分页 polyfill；CLI 最终仍借助 Puppeteer 输出 PDF | 高 | 中高 | 高 | 不能单独解决渲染问题 |
| [pdfmake](https://github.com/bpampuch/pdfmake) | JS 文档定义 → PDF | 低 | 高 | 低中 | 需重写报告排版 |
| [PDFKit](https://github.com/foliojs/pdfkit) | JS 手工绘制文本、图形、图片 | 低 | 高 | 低中 | 需重写整个报告渲染器 |

### 4.1 推荐：Playwright 后端渲染

Playwright 的 PDF 方式是浏览器原生打印布局，不依赖 html2canvas 的像素复制，因此最接近用户看到的报告页面。实施时不应让后端重新拼一套“近似报告”，而应复用前端结构化数据和打印 CSS：

```text
前端将当前报告 DOM/CSS 序列化为一次性 HTML
  → 后端建立临时渲染输入
  → Playwright 在隔离页面加载 HTML
  → 等待字体、ECharts、图片和网络空闲
  → page.pdf(A4, printBackground)
  → 返回 PDF 流
```

本项目已按“不增加 Docker 渲染服务”的约束落地：Playwright 作为现有 `backend/scripts` 的 Node 依赖，由 Flask 通过一次性临时输入文件调用渲染脚本。Windows 优先使用本机 Chrome，也支持通过 `MIAOYU_CHROMIUM_PATH` 指定 Chromium/Chrome；首次源码部署需要执行 `npx playwright install chromium`。这样保持部署边界清晰，同时保留后续切换独立渲染服务的接口空间。

### 4.2 WeasyPrint 何时采用

如果后续需要减少 Node/Chromium 资源，可以评估 WeasyPrint。它直接接受 HTML/CSS 并支持 `@page` 分页，但当前项目需要额外处理：

- ECharts 必须在前端转成 SVG/PNG data URL 或由后端生成静态图；
- Docker 必须加入 Cairo、Pango、字体等运行库；
- 需要显式安装中文字体，否则会出现缺字或回退字体不一致；
- 需要处理图片 URL 加载和 Flask 单线程服务阻塞问题；
- 现有部分浏览器 CSS 需要按 WeasyPrint 的支持范围重验。

所以它适合作为第二实现，不适合作为本次空白故障的最快替换。

## 5. 分阶段交付规划

### P0：止损与验证（已完成）

- 移除前端对 html2pdf 的调用，避免继续下载“成功但全白”的 PDF；
- 清理诊断探针；
- 增加 Playwright 启动失败、超时和无效 PDF 的可读错误；
- 已完成最小 HTML 和 Flask 接口集成测试。

### P1：Playwright 最小闭环（已完成）

- 新增 `/api/report/export-pdf`；
- 后端接收一次性 HTML 导出内容，不新增可被外部访问的报告打印路由；
- 新增 `backend/scripts/render_pdf.mjs`，直接复用现有 Node 运行环境，不增加 Docker 服务；
- 等待 `document.fonts.ready`、图表完成、图片加载后调用 `page.pdf()`；
- A4 打印布局保留三图横向仪表盘，观点卡改为单列防拆，避免屏幕三列布局被错误拉伸成宽卡片；
- 返回 `application/pdf`，文件名沿用现有安全化规则；
- 不改变 report JSON、Word、Markdown 和历史归档接口。

### P2：交付级稳定性

- 中文字体固定版本并纳入镜像；
- 导出超时、并发上限、浏览器进程回收和健康检查；
- 失败时保留可读错误，不返回空 PDF；
- 覆盖单页、跨页、长 URL、暗色页面、无图表、含图表、移动端触发导出；
- 通过 PDF 文本提取和渲染截图双重验收，禁止只检查 HTTP 200。

## 6. 接口预留

```http
POST /api/report/export-pdf
Content-Type: application/json

{
  "html": "<article>...</article>",
  "filename": "舆情分析报告.pdf"
}
```

响应要求：

- 成功：`200 application/pdf`，`Content-Disposition` 提供安全文件名；
- 报告不存在/结构损坏：`404/400`；
- 渲染超时或服务不可用：`503`，JSON 返回可操作错误；
- 禁止 `200` 返回空白 PDF；后端至少检查 PDF 页数和内容对象，前端再做下载。

## 7. 当前决策

本轮已按“不增加 Docker 渲染服务”的约束完成 P0/P1 代码接入：Node Playwright 依赖写入 `backend/scripts/package.json`，渲染脚本支持 `MIAOYU_CHROMIUM_PATH`，后端通过 `/api/report/export-pdf` 返回有效 PDF。真实报告已完成自动化浏览器下载验证（6 页、约 466 KB）；下一步是用户浏览器人工验收和 P2 稳定性补强，不再回到 html2canvas 截图链路。
