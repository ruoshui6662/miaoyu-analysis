# 妙舆 fnOS FPK 源包

这是由 `fnpack` 构建的 Docker 应用源目录，不是直接可安装的 `.fpk` 文件。

## 构建

在项目根目录执行：

```bash
python scripts/build_fpk.py --dry-run
python scripts/build_fpk.py
```

前提是已安装飞牛官方 `fnpack`，或设置 `FNPACK_BIN` 指向该工具。

## 运行约束

- 运行时从 GHCR 拉取 `ghcr.io/ruoshui6662/miaoyu-analysis:latest`，目标设备必须能访问镜像仓库。
- 数据写入 `${TRIM_PKGVAR}/data`，升级时不随包覆盖。
- 配置写入 `${TRIM_PKGETC}/.env`，安装回调只在文件不存在时生成模板，不覆盖用户配置。
- FPK 源包不包含 `.env`、数据目录和任何真实密钥。
- `platform=all` 只表示 FPK 本身没有架构专属二进制；仍需确认 GHCR 镜像支持目标 fnOS 架构。
