# 妙舆 fnOS FPK 打包说明

本目录采用飞牛官方 Docker 应用包结构。FPK 负责在 fnOS 应用中心注册应用、管理 Docker Compose 项目和桌面入口；妙舆运行时仍由 GHCR 镜像提供。

## 源包位置

```text
fpk/miaoyu/
├── manifest
├── app/
│   ├── docker/docker-compose.yaml
│   └── ui/config
├── cmd/
│   ├── main
│   ├── install_callback
│   └── upgrade_callback
└── config/
    ├── privilege
    └── resource
```

## 本地构建

先下载飞牛官方 `fnpack`，然后在项目根目录执行：

```bash
python scripts/build_fpk.py --dry-run
python scripts/build_fpk.py
```

也可以显式指定工具路径：

```bash
python scripts/build_fpk.py --fnpack /path/to/fnpack --version 0.1.1
```

成功后产物位于 `dist/miaoyu-<version>.fpk`。当前开发机未安装 `fnpack`，因此仓库只提交 FPK 源包与构建脚本，不提交二进制工具和 `.fpk` 产物。

## 配置与数据

- 容器镜像：`ghcr.io/ruoshui6662/miaoyu-analysis:latest`。
- 服务端口：manifest 默认 `5000`，由 `${TRIM_SERVICE_PORT}` 映射到容器 `5000`。
- 运行数据：`${TRIM_PKGVAR}/data`，用于 SQLite、报告、任务和原始素材，升级不覆盖。
- 配置文件：`${TRIM_PKGETC}/.env`，首次安装从模板生成，升级时保留用户现有文件。
- `.env` 中的 AI Key、Bearer 管理员 Token 等敏感信息不进入 Git 或 FPK；网页登录密码保存在运行数据目录的 `admin_account.json` 哈希文件中。

首次安装后，在 fnOS 应用配置目录生成 `.env`，按需填写 `MIAOYU_ADMIN_TOKEN`（至少 6 个字符）和需要的 AI/SearXNG 配置，再启动应用。网页登录账号固定为 `admin`，初始密码为 `password`；首次登录后必须在“设置 → 账户管理”修改并保存。

## 飞牛侧验收

在应用中心手动安装，或使用：

```bash
appcenter-cli install-fpk miaoyu-0.1.0.fpk
```

逐项检查：安装成功、镜像能拉取、应用能启动、桌面入口能打开、`/healthz` 返回 200、热榜和登录可用、重启后数据仍在、升级不会覆盖 `.env`。目标设备还要确认镜像实际支持其 CPU 架构。

本阶段已完成本地源包契约验收；真实 `.fpk` 构建和 fnOS 安装验收仍未执行。

官方参考：[fnpack CLI](https://developer.fnnas.com/docs/cli/fnpack)、[Docker 应用案例](https://developer.fnnas.com/docs/examples/docker)、[Manifest](https://developer.fnnas.com/docs/core-concepts/manifest)。
