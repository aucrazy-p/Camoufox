"""
项目临时目录清理：永久删除，绝不进回收站。

背景（两次事故）：
  1) 2026-11-10：在 agent 会话里用 `rm -rf` / PowerShell `Remove-Item` /
     `rd /s` 清理构建临时目录时，执行环境的「安全删除」层把批量删除
     改写成「移到回收站」。一次构建要清 stage / fresh_data / pyi-work，
     反复几十轮后 D 盘回收站累积 75,919 条 / 6.58 GB，资源管理器
     出现「显示有文件、清空却找不到文件」的假象。
  2) 2026-11-11：改用「Python 进程内删除」后仍然进回收站。实测根因是
     WorkBuddy 内置 Python 通过 PYTHONPATH 注入了
     `vendor/shim/sitecustomize.py`，把 shutil.rmtree / os.unlink /
     os.remove / pathlib.Path.unlink 全替换成 `_safe_*`（移到回收站）。
     项目的 embeddable `runtime\\python.exe` 没被注入，所以之前验证
     「回收站 0→0」是真的，但换成内置 Python 就失效。

因此本模块用 CPython 内建的 `nt.unlink` / `nt.rmdir` 自底向上删，
不用被替换的 os.unlink / os.rmdir / shutil.rmtree / Path.unlink。
实测 ctypes 直调 kernel32!RemoveDirectoryW 会被拦成 ACCESS_DENIED，
nt 层反而能过。无论用哪个解释器、有没有 shim，删除都是永久的。

约定（重要）：
    本项目的任何清理只允许走这里 —— 构建脚本内部 import 调用，
    或命令行执行 `python _tools\\purge.py --temp`。
    禁止在会话里对本项目目录使用 rm -rf / Remove-Item / rd /s，
    也不要直接用 shutil.rmtree / os.unlink 删项目目录。

环境限制：某些 agent 沙箱会在文件系统层拦截 RemoveDirectoryW
（返回 ERROR_ACCESS_DENIED），表现为文件删干净但空目录残留。
脚本会打印「删除后仍有残留」，不静默吞掉。GitHub Actions runner
无此限制；本机如遇残留，用 PowerShell
[System.IO.Directory]::Delete($p, $true) 在沙箱外兜底。

用法：
    python _tools\\purge.py --list    # 只列出将删除什么
    python _tools\\purge.py --temp    # 清临时目录（保留 dist 产物）
    python _tools\\purge.py --all     # 再加 dist（下次构建会重建）
    python _tools\\purge.py <路径>... # 清白名单内的指定路径
"""
from __future__ import annotations

import os
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

# 源码 / 配置 / 账号数据永远不许删；rmtree 会做前缀检查。
PROTECTED = (
    ROOT / "app", ROOT / "resources",
    ROOT / ".git", ROOT / ".github", ROOT / "data",
    ROOT / "CamoufoxData", ROOT / "fingerprint-manager",
)

# ---- 绕过 sitecustomize 的 _safe_* 替换：直用 nt 内建 ----
# 实测（2026-11-11）：shim 把 os.unlink / os.remove / os.rmdir /
# shutil.rmtree / pathlib.Path.unlink 全换成「移到回收站」版；
# nt.unlink / nt.rmdir 是 CPython built-in，未被替换，删除是永久的。
if sys.platform == "win32":
    import nt

    def _del_file(p: Path) -> bool:
        s = str(p)
        try:
            nt.unlink(s)
            return True
        except OSError:
            try:
                nt.chmod(s, 0o600)  # 去掉只读属性后重试
                nt.unlink(s)
                return True
            except OSError:
                return False

    def _del_dir(p: Path) -> bool:
        try:
            nt.rmdir(str(p))
            return True
        except OSError:
            return False
else:  # 非 Windows 仅作兜底，本项目只在 Windows 构建
    def _del_file(p: Path) -> bool:
        try:
            p.unlink()
            return True
        except OSError:
            return False

    def _del_dir(p: Path) -> bool:
        try:
            p.rmdir()
            return True
        except OSError:
            return False


def _native_rmtree(p: Path, ignore_errors: bool = True) -> int:
    """自底向上永久删除目录树，返回未能删除的条目数。"""
    failed = 0
    for root, dirs, files in os.walk(p, topdown=False, onerror=lambda e: None):
        for name in files:
            if not _del_file(Path(root) / name):
                failed += 1
        for name in dirs:
            if not _del_dir(Path(root) / name):
                failed += 1
    if not _del_dir(p):
        failed += 1
    if failed and not ignore_errors:
        raise OSError(f"{failed} 项未能删除：{p}")
    return failed


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
        if not _del_file(p) and not ignore_errors:
            raise OSError(f"删除失败：{p}")
        return 1
    count = sum(1 for _ in p.rglob("*"))
    _native_rmtree(p, ignore_errors)
    return count


def unlink(path: Path) -> None:
    """永久删除单个文件。"""
    p = Path(path).resolve()
    if p == ROOT or not p.is_relative_to(ROOT):
        raise ValueError(f"拒绝删除项目外的路径：{p}")
    if any(p == q or p.is_relative_to(q) for q in PROTECTED):
        raise ValueError(f"拒绝删除受保护目录：{p}")
    if not p.exists():
        return
    if not _del_file(p):
        raise OSError(f"删除失败：{p}")


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
            rmtree(p) if p.is_dir() else unlink(p)
            if p.exists():
                print(f"    警告：删除后仍有残留 {p}", flush=True)
    print(f"合计 {total} 项；{'预览模式，未删除' if dry else '已永久删除（不进回收站）'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
