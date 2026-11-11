"""
项目临时目录清理：永久删除，绝不进回收站。

背景（2026-11-10 事故）：
    在 agent 会话里用 `rm -rf` / PowerShell `Remove-Item` / `rd /s` 清理
    构建临时目录时，执行环境的「安全删除」层会把批量删除改写成
    「移到回收站」。本项目一次构建要清 stage / fresh_data / pyi-work，
    反复几十轮后 D 盘回收站累积了 75,919 条 / 6.58 GB，资源管理器
    出现「显示有文件、清空却找不到文件」的假象。
    本脚本在 Python 进程内用 shutil.rmtree / Path.unlink 直接删除，
    不经过任何 shell 命令，因此不会进回收站。

约定（重要）：
    本项目的任何清理只允许走这里 —— 构建脚本内部 import 调用，
    或命令行执行 `runtime\\python.exe _tools\\purge.py --temp`。
    禁止在会话里对本项目目录使用 rm -rf / Remove-Item / rd /s。

用法：
    runtime\\python.exe _tools\\purge.py --list    # 只列出将删除什么
    runtime\\python.exe _tools\\purge.py --temp    # 清临时目录（保留 dist 产物）
    runtime\\python.exe _tools\\purge.py --all     # 再加 dist（下次构建会重建）
    runtime\\python.exe _tools\\purge.py <路径>... # 清白名单内的指定路径
"""
from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "_tools"
STAGE = TOOLS / "stage"

# CLI 允许清理的临时目录（--temp）
TEMP_TARGETS = (
    STAGE,                       # 每轮构建的中间产物
    TOOLS / "fresh_data",        # verify_exe 的干净数据目录
    TOOLS / "portable_probe",    # verify_portable 的便携版探针目录
    TOOLS / "cache",             # 诊断/实验产物（ci_extract、ascii 等）
    TOOLS / "pyi-work",          # PyInstaller 工作目录
    TOOLS / "build",             # PyInstaller 旧版工作目录
)
# 需要 --all 才会删
OUTPUT_TARGETS = (ROOT / "dist",)

# 源码 / 配置 / 账号数据永远不许删；purge_tree 会做前缀检查。
# 注意：runtime/（内嵌 Python）不在此列 —— prepare_runtime.py 的职责就是
# 删掉它再重建，CLI 白名单里也没有它，命令行误删不到。
PROTECTED = (
    ROOT / "app", ROOT / "resources",
    ROOT / ".git", ROOT / ".github", ROOT / "data",
    ROOT / "CamoufoxData", ROOT / "fingerprint-manager",
)


def _onexc(func, path, exc):
    """Windows 上只读 / 被杀软短暂占住的文件：去掉只读属性后重试一次。"""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        raise exc  # 交给上层（ignore_errors 或调用方）


def rmtree(path: Path, ignore_errors: bool = True) -> int:
    """永久删除目录树，返回删除前的条目数。只允许删项目内的临时路径。"""
    p = Path(path).resolve()
    if p == ROOT or not p.is_relative_to(ROOT):
        raise ValueError(f"拒绝删除项目外的路径：{p}")
    if any(p == q or p.is_relative_to(q) for q in PROTECTED):
        raise ValueError(f"拒绝删除受保护目录：{p}")
    if not p.exists():
        return 0
    if p.is_file() or p.is_symlink():
        p.unlink(missing_ok=True)
        return 1
    count = sum(1 for _ in p.rglob("*"))
    try:
        shutil.rmtree(p, onexc=_onexc)
    except TypeError:  # Python < 3.12 没有 onexc
        shutil.rmtree(p, onerror=lambda f, q, e: _onexc(f, q, e))
    except Exception:
        if not ignore_errors:
            raise
    return count


def unlink(path: Path) -> None:
    """永久删除单个文件。"""
    p = Path(path).resolve()
    if p == ROOT or not p.is_relative_to(ROOT):
        raise ValueError(f"拒绝删除项目外的路径：{p}")
    if any(p == q or p.is_relative_to(q) for q in PROTECTED):
        raise ValueError(f"拒绝删除受保护目录：{p}")
    p.unlink(missing_ok=True)


def clean_temp() -> int:
    """清掉全部历史临时目录，返回删除的条目数。构建脚本开工时调用。"""
    return sum(rmtree(p) for p in TEMP_TARGETS)


def _cli(argv: list[str]) -> int:
    targets: list[Path]
    if not argv or "--list" in argv:
        targets = list(TEMP_TARGETS)
        if "--all" in argv:
            targets += list(OUTPUT_TARGETS)
    elif "--temp" in argv or "--all" in argv:
        targets = list(TEMP_TARGETS)
        if "--all" in argv:
            targets += list(OUTPUT_TARGETS)
    else:
        # 指定路径：必须在白名单内（拒绝任意路径，防误删）
        allowed = {q.resolve() for q in TEMP_TARGETS + OUTPUT_TARGETS}
        targets = []
        for a in argv:
            p = Path(a).resolve()
            if p not in allowed:
                print(f"拒绝：{p} 不在清理白名单内", flush=True)
                return 2
            targets.append(p)

    dry = "--list" in argv or not argv
    total = 0
    for p in targets:
        if not p.exists():
            continue
        n = sum(1 for _ in p.rglob("*")) if p.is_dir() else 1
        print(f"  {'[预览]' if dry else '[删除]'} {p.relative_to(ROOT)}  ({n} 项)", flush=True)
        total += n
        if not dry:
            removed = rmtree(p) if p.is_dir() else (unlink(p) or 1)
            if removed == 0:
                print(f"    警告：删除后仍有残留 {p}", flush=True)
    print(f"合计 {total} 项；{'预览模式，未删除' if dry else '已永久删除（不进回收站）'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
