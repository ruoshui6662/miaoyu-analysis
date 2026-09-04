# 妙舆 Logo 本地资产库

本目录由 `tools/fetch-logo-library.ps1` 生成，目标是把运行时不稳定的远程 favicon 逐步固化为本地资产。

## 抓取顺序

1. 已核验品牌映射：Simple Icons 的 SVG。
2. 信源官网声明的 favicon / apple-touch-icon / 页面 icon 链接。
3. NAS `http://192.168.68.112:50560/` 的 HD-Icons，仅接受名称与来源高置信匹配的 SVG 候选。

脚本会对响应做 PNG/JPEG/GIF/WebP/ICO/SVG 内容签名校验；返回 HTML、登录页、错误页或空响应不会写入库。`manifest.json` 是逐来源审计清单，包含 provider、source_url、status、许可提示和检查时间。

## 本地执行

先启动本地服务，再执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\fetch-logo-library.ps1
```

如果当前服务使用显式管理员令牌，可通过参数传入；令牌不会写入清单：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\fetch-logo-library.ps1 -ApiToken "你的本地令牌"
```

NAS 不可用时可跳过：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\fetch-logo-library.ps1 -SkipNas
```

`pending` 不代表没有图标，而是没有找到可验证、可稳定托管的资产；前端继续使用已有品牌 sprite 或统一分类图标兜底。NAS 候选即便抓取成功，也需要人工复核后再把状态视为已核验。

## 许可边界

- Simple Icons：以项目仓库声明为依据记录 CC0；品牌本身的商标权仍归原权利人。
- 官网 favicon：只作为来源识别，入库前应确认对应站点的品牌和使用规则。
- NAS HD-Icons：当前仅能确认是局域网资产库，许可信息写为“待确认”，不自动宣称可商用。
- Iconfont、Flaticon、Icons8、The Noun Project 不作为运行时自动抓取源；它们适合人工选材，具体授权需逐项核验。
