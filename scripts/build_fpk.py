"""构建妙舆 fnOS FPK。

只把 fpk/miaoyu 作为源包交给飞牛官方 fnpack；不读取或打包仓库 .env、data
等运行态内容，避免把部署密钥带入应用包。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "fpk" / "miaoyu"
DIST = ROOT / "dist"
REQUIRED_FILES = (
    "manifest",
    "config/privilege",
    "config/resource",
    "cmd/main",
    "cmd/install_callback",
    "cmd/upgrade_callback",
    "app/docker/docker-compose.yaml",
    "app/docker/.env.example",
    "app/ui/config",
    "ICON.PNG",
    "ICON_256.PNG",
)
REQUIRED_DIRS = ("app", "cmd", "config", "wizard")
FORBIDDEN_PARTS = {".git", "data", "node_modules", ".venv", "venv"}


def manifest_value(text: str, key: str) -> str:
    match = re.search(rf"(?m)^\s*{re.escape(key)}\s*=\s*(.*?)\s*$", text)
    if not match:
        raise ValueError(f"manifest 缺少 {key}")
    return match.group(1).strip().strip('"')


def validate_source(source: Path) -> tuple[str, str]:
    missing_dirs = [item for item in REQUIRED_DIRS if not (source / item).is_dir()]
    if missing_dirs:
        raise ValueError("FPK 源目录缺少目录：" + ", ".join(missing_dirs))
    missing = [item for item in REQUIRED_FILES if not (source / item).is_file()]
    if missing:
        raise ValueError("FPK 源目录缺少：" + ", ".join(missing))

    manifest = (source / "manifest").read_text(encoding="utf-8")
    appname = manifest_value(manifest, "appname")
    version = manifest_value(manifest, "version")
    if appname != "miaoyu":
        raise ValueError(f"appname 必须为 miaoyu，实际为 {appname!r}")
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]*", version):
        raise ValueError(f"版本号格式不合法：{version!r}")

    compose = (source / "app/docker/docker-compose.yaml").read_text(encoding="utf-8")
    for required in ("TRIM_SERVICE_PORT", "TRIM_PKGVAR", "TRIM_PKGETC", "ghcr.io/ruoshui6662/miaoyu-analysis"):
        if required not in compose:
            raise ValueError(f"Docker Compose 缺少预期契约：{required}")
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        if any(part in FORBIDDEN_PARTS for part in relative.parts):
            raise ValueError(f"FPK 源包包含禁止目录：{relative}")
        if path.name == ".env" or path.name.endswith(".fpk"):
            raise ValueError(f"FPK 源包包含禁止文件：{relative}")
    return appname, version


def resolve_fnpack(explicit: str | None) -> str | None:
    candidate = explicit or os.environ.get("FNPACK_BIN")
    if candidate:
        return candidate
    return shutil.which("fnpack")


def build(fnpack: str, requested_version: str | None) -> Path:
    appname, source_version = validate_source(SOURCE)
    version = requested_version or source_version
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]*", version):
        raise ValueError(f"版本号格式不合法：{version!r}")

    with tempfile.TemporaryDirectory(prefix="miaoyu-fpk-") as temp:
        staging = Path(temp) / appname
        shutil.copytree(SOURCE, staging)
        manifest_path = staging / "manifest"
        manifest = manifest_path.read_text(encoding="utf-8")
        manifest = re.sub(r"(?m)^(\s*version\s*=\s*).*$", rf"\g<1>{version}", manifest)
        manifest_path.write_text(manifest, encoding="utf-8")

        subprocess.run([fnpack, "build"], cwd=staging, check=True)
        packages = sorted(staging.glob("*.fpk"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not packages:
            raise RuntimeError("fnpack 执行成功但没有生成 .fpk 文件")
        DIST.mkdir(parents=True, exist_ok=True)
        output = DIST / f"{appname}-{version}.fpk"
        shutil.copy2(packages[0], output)
        return output


def main() -> int:
    parser = argparse.ArgumentParser(description="构建妙舆 fnOS FPK")
    parser.add_argument("--fnpack", help="fnpack 可执行文件路径；也可使用 FNPACK_BIN")
    parser.add_argument("--version", help="覆盖 manifest 中的版本号")
    parser.add_argument("--dry-run", action="store_true", help="只执行源包契约检查，不调用 fnpack")
    args = parser.parse_args()
    try:
        appname, version = validate_source(SOURCE)
        print(f"FPK 源包检查通过：{appname} {version}")
        if args.dry_run:
            print("dry-run：未调用 fnpack，未生成 .fpk")
            return 0
        fnpack = resolve_fnpack(args.fnpack)
        if not fnpack:
            print("未找到 fnpack；请从飞牛官方 CLI 下载后设置 FNPACK_BIN。", file=sys.stderr)
            return 2
        output = build(fnpack, args.version)
        print(f"FPK 已生成：{output}")
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"FPK 构建失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
