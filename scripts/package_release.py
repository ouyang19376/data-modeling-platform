"""发布打包：将源代码 + 文档 + 演示数据库打成版本 zip 放到项目根目录。

用法：
    python scripts/package_release.py v1.0

产物：<项目根>/data-modeling-center-<version>.zip，内含顶层目录
data-modeling-center-<version>/，解压后按 README 快速开始即可运行。

打包范围（白名单）：
    backend/ frontend/ tests/ migrations/ scripts/ docs/
    README.md alembic.ini requirements.txt VERSION（生成）
    data/platform.db（已播种的演示库，开箱即用；启动播种仅在空库时生效，不会重复注入）

排除（运行时产物 / 临时文件）：
    __pycache__ *.pyc *.bak *.log .pytest_cache
    .workbuddy/ .playwright-cli/ .playwright-cli-e2e/ .git/ node_modules/
    data/ 下除 platform.db 外的所有库（dev/smoke/备份）
    根目录浏览器测试残留（s1.yaml s2.yaml dashboard.png models.png
    screenshot_v09_lineage.png）
"""
from __future__ import annotations

import sys
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INCLUDE_DIRS = ("backend", "frontend", "tests", "migrations", "scripts", "docs")
INCLUDE_ROOT_FILES = ("README.md", "alembic.ini", "requirements.txt")
INCLUDE_DATA_FILES = ("platform.db",)
EXCLUDE_DIR_NAMES = {
    "__pycache__", ".pytest_cache", ".workbuddy", ".playwright-cli",
    ".playwright-cli-e2e", ".git", "node_modules", ".venv", "venv",
    ".idea", ".vscode", ".mypy_cache", ".ruff_cache",
}
EXCLUDE_FILE_SUFFIXES = (".pyc", ".bak", ".log", ".tmp", ".DS_Store")
EXCLUDE_ROOT_STRAY = (
    "s1.yaml", "s2.yaml", "dashboard.png", "models.png",
    "screenshot_v09_lineage.png",
)


def should_skip_dir(rel: Path) -> bool:
    return any(part in EXCLUDE_DIR_NAMES for part in rel.parts)


def should_skip_file(rel: Path) -> bool:
    if rel.suffix in EXCLUDE_FILE_SUFFIXES:
        return True
    if rel.parent == Path(".") and rel.name in EXCLUDE_ROOT_STRAY:
        return True
    return False


def build(version: str) -> Path:
    pkg_name = f"data-modeling-center-{version}"
    out = ROOT / f"{pkg_name}.zip"
    version_text = (
        f"数据建模中心（Data Modeling Center）\n"
        f"版本：{version}\n"
        f"发布日期：{date.today().isoformat()}\n"
        f"内容：backend/ frontend/ tests/ migrations/ scripts/ docs/ "
        f"+ 演示数据库（PRJ_ECOM 电商数仓 114 模型）\n"
        f"启动：pip install -r requirements.txt && "
        f"python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000\n"
    )

    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr(f"{pkg_name}/VERSION", version_text)
        count += 1

        def add(abs_path: Path, rel: Path) -> None:
            nonlocal count
            zf.write(abs_path, f"{pkg_name}/{rel.as_posix()}")
            count += 1

        # 白名单目录
        for d in INCLUDE_DIRS:
            base = ROOT / d
            if not base.is_dir():
                continue
            for p in sorted(base.rglob("*")):
                rel = p.relative_to(ROOT)
                if p.is_dir():
                    if should_skip_dir(rel):
                        continue
                    continue
                if should_skip_dir(rel.parent) or should_skip_file(rel):
                    continue
                add(p, rel)

        # 根目录文件
        for name in INCLUDE_ROOT_FILES:
            p = ROOT / name
            if p.is_file():
                add(p, Path(name))

        # 演示数据库
        for name in INCLUDE_DATA_FILES:
            p = ROOT / "data" / name
            if p.is_file():
                add(p, Path("data") / name)

    size_mb = out.stat().st_size / 1024 / 1024
    print(f"[打包完成] {out}")
    print(f"  文件数：{count}，大小：{size_mb:.2f} MB")
    return out


if __name__ == "__main__":
    ver = sys.argv[1] if len(sys.argv) > 1 else "v1.0"
    if not ver.startswith("v"):
        ver = f"v{ver}"
    build(ver)
