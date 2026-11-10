"""
打包控制端为单文件 exe（不含浏览器内核、不含任何账号数据）。

用法：  runtime\\python.exe _tools\\build.py

产物：  dist\\指纹环境管理器.exe

白名单原则：
  - 进包：内嵌 Python 运行时（剔除 pip/setuptools/PyInstaller 等开发工具）、
          app 代码与静态资源、图标。字体裁剪清单已停用，不再进包。
  - 不进包：data\\ 下的 app.db / profiles / cookies / host_gpu.json / kernel /
            Edge 误写进来的 619MB 用户配置；_dl / _dev / _trim / _tools。
程序文件每次解压到 exe 同目录的 CamoufoxData\\tmp\\ 里（退出即清理，
不在 exe 旁边凭空多出一个 _MEIxxxxxx 文件夹），账号数据也在 CamoufoxData\\ ——
不写 C 盘、整个文件夹可搬走。
所以换 exe = 升级控制端，账号一个都不会丢。
"""
from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "_tools"
STAGE = TOOLS / "stage"
DIST = ROOT / "dist"
# 本机默认用内嵌 Python 跑 PyInstaller；CI 上 runtime/ 只是待打包的数据，
# 用 setup-python 的解释器更稳，可用 FP_BUILD_PY 指过去（本地行为不变）。
PY = Path(os.environ.get("FP_BUILD_PY") or (ROOT / "runtime" / "python.exe"))
EXE_NAME = "指纹环境管理器"

# 只在开发机上用得到，不进成品
DROP_PATTERNS = [
    "pip", "pip-*", "setuptools", "setuptools-*", "_distutils_hack",
    "PyInstaller", "pyinstaller-*", "pyinstaller_hooks_contrib*",
    "altgraph*", "pefile*", "pywin32_ctypes*", "_pyinstaller_hooks_contrib*",
    "pkg_resources", "wheel*", "__pycache__", "*.pyc", "*.pyo",
    "Scripts", "*.egg-info", "*.dist-info/RECORD", "*.dist-info/WHEEL",
    # 这个 .pth 会去 import 已被剔除的 _distutils_hack，导致 CLI 每次都打印一段警告
    "distutils-precedence.pth",
]
DROP_TOP_DIRS = {"Scripts", "__pycache__"}


def _drop(name: str) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in DROP_PATTERNS)


def _copy_tree(src: Path, dst: Path, is_site: bool = False) -> int:
    n = 0
    dst.mkdir(parents=True, exist_ok=True)
    for p in src.iterdir():
        if p.name in DROP_TOP_DIRS or _drop(p.name):
            continue
        if p.is_dir():
            n += _copy_tree(p, dst / p.name, is_site=False)
        elif p.is_file():
            if _drop(p.name):
                continue
            shutil.copy2(p, dst / p.name)
            n += 1
    return n


def prepare_stage() -> dict:
    if STAGE.exists():
        shutil.rmtree(STAGE, ignore_errors=True)
    STAGE.mkdir(parents=True)

    runtime_dst = STAGE / "runtime"
    n_runtime = _copy_tree(ROOT / "runtime", runtime_dst, is_site=True)
    # site-packages 里按名字剔除开发工具
    site = runtime_dst / "Lib" / "site-packages"
    removed = 0
    if site.exists():
        for p in list(site.iterdir()):
            if _drop(p.name):
                shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
                removed += 1

    app_dst = STAGE / "app"
    app_dst.mkdir(parents=True)
    n_app = 0
    for p in (ROOT / "app").iterdir():
        if p.name == "__pycache__":
            continue
        if p.is_dir() and p.name in ("static", "assets"):
            shutil.copytree(p, app_dst / p.name, dirs_exist_ok=True)
            n_app += sum(1 for _ in (app_dst / p.name).rglob("*") if _.is_file())
        elif p.is_file() and p.suffix in (".py", ".txt", ".json"):
            shutil.copy2(p, app_dst / p.name)
            n_app += 1

    res_dst = STAGE / "resources"
    res_src = ROOT / "resources"
    if res_src.exists():
        shutil.copytree(res_src, res_dst, dirs_exist_ok=True)
    else:
        res_dst.mkdir(parents=True, exist_ok=True)
    # 字体裁剪已取消，两份按文件名维护的黑白名单不再进成品（仅留开发目录作历史记录）
    for legacy in ("fonts_not_win.txt", "fonts_win.txt"):
        (res_dst / legacy).unlink(missing_ok=True)
    n_res = sum(1 for _ in res_dst.rglob("*") if _.is_file())

    return {"runtime_files": n_runtime, "dropped": removed, "app_files": n_app, "resources": n_res}


def build_exe() -> Path:
    DIST.mkdir(exist_ok=True)
    cmd = [
        str(PY), "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", EXE_NAME,
        "--icon", str(ROOT / "app" / "assets" / "icon.ico"),
        # 便携版：bootloader 自动创建这个多级目录并解压，退出后只删 _MEIxxxxxx。
        # 藏进 CamoufoxData\\tmp，exe 旁边就只剩数据文件夹，也不会走系统 %TEMP%。
        "--runtime-tmpdir", "CamoufoxData/tmp",
        "--add-data", f"{STAGE / 'runtime'};runtime",
        "--add-data", f"{STAGE / 'app'};app",
        "--add-data", f"{STAGE / 'resources'};resources",
        "--distpath", str(DIST),
        "--workpath", str(TOOLS / "pyi-work"),
        "--specpath", str(TOOLS),
        str(TOOLS / "boot.py"),
    ]
    print("执行打包…", flush=True)
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    tail = "\n".join((r.stdout or "").splitlines()[-18:])
    print(tail, flush=True)
    if r.returncode != 0:
        print((r.stderr or "")[-3000:], flush=True)
        raise RuntimeError(f"PyInstaller 失败，退出码 {r.returncode}")
    exe = DIST / f"{EXE_NAME}.exe"
    if not exe.exists():
        raise RuntimeError("打包完成但没找到 exe")
    return exe


def verify(exe: Path) -> None:
    """列 PKG 内容，确认关键文件在、账号数据绝对不在。"""
    from PyInstaller.archive.readers import CArchiveReader

    reader = CArchiveReader(str(exe))
    names = list(reader.toc.keys())
    must = ["runtime/pythonw.exe", "runtime/python.exe", "app/main.py",
            "app/policy.py", "app/static/index.html", "app/static/app.js"]
    for m in must:
        hit = [n for n in names if n.replace("\\", "/").endswith(m)]
        print(f"  {'OK ' if hit else 'MISS'} {m}")
        if not hit:
            raise RuntimeError(f"包内缺少 {m}")

    forbidden = ("app.db", "cookies.sqlite", "host_gpu.json", "parent.lock",
                 "logins.json", "sessionstore", "Microsoft/Edge", "fonts-not-win")
    bad = [n for n in names if any(f.lower() in n.replace("\\", "/").lower() for f in forbidden)]
    print(f"  敏感/冗余文件命中: {len(bad)}")
    if bad:
        print("   ", bad[:10])
        raise RuntimeError("包内混入了账号数据或冗余文件")
    print(f"  PKG 条目 {len(names)}，exe {exe.stat().st_size/1024/1024:.1f} MB")


def main() -> int:
    t0 = time.time()
    print("=== 1/3 准备白名单文件 ===", flush=True)
    info = prepare_stage()
    print(f"  {info}", flush=True)
    print("=== 2/3 PyInstaller 打包 ===", flush=True)
    exe = build_exe()
    print("=== 3/3 校验产物 ===", flush=True)
    verify(exe)
    print(f"\n完成，用时 {time.time()-t0:.0f}s\n  {exe}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("打包失败:", type(exc).__name__, exc, flush=True)
        sys.exit(1)
