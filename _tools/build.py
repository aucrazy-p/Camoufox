"""
打包控制端，一次构建同时产出「管理端 exe」与「浏览器运行时包」。

用法：  runtime\\python.exe _tools\\build.py

产物（同一次构建一起生成，不需要额外下载/额外跑一遍）：
  dist\\指纹环境管理器.exe              约 20 MB，只含管理端
  dist\\runtime-extra-v1.zip           约 70 MB，浏览器运行时
  dist\\runtime-extra-v1.zip.sha256    校验值

拆分原则：
  - exe 只留管理端需要的依赖（FastAPI / uvicorn / WebView2 / 托盘 / requests）。
    Playwright 的 Node driver、numpy/fpgen、camoufox 等全拆到 zip，
    首次使用由用户在界面手动下载（或指定本地 zip 离线装）。
  - 采用「拆出去的显式清单」，其余一律留在 exe —— 绝不误拆管理端启动期依赖。
    verify() 会断言 exe 里没有 playwright/numpy/camoufox/fpgen。
  - 不进包：data\\ 下的 app.db / profiles / cookies / host_gpu.json / kernel /
    Edge 误写进来的 619MB 用户配置；_dl / _dev / _trim / _tools。
程序文件每次解压到 exe 同目录的 CamoufoxData\\tmp\\ 里（退出即清理），
账号数据也在 CamoufoxData\\ —— 不写 C 盘、整个文件夹可搬走。
所以换 exe = 升级控制端，账号和已装运行时都不会丢。
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

# Windows 控制台默认 cp1252/cp936；CI runner 是 cp1252，中文 print 会
# UnicodeEncodeError 直接崩。强制 UTF-8 输出。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "_tools"
STAGE = TOOLS / "stage"
DIST = ROOT / "dist"
# 拆出去的运行时包放在这里；PyInstaller 只 add-data STAGE/runtime，不会带进 exe
EXTRA_STAGE = STAGE / "runtime-extra"
# 版本锁定：exe 内置这个 tag，不追 latest；升级依赖时才改常量发新 tag
RUNTIME_TAG = "runtime-v1"
PACK_ZIP = "runtime-extra-v1.zip"
PACK_SHA = PACK_ZIP + ".sha256"
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

# ---------------------------------------------------------------- 运行时拆分清单
# 这些是 site-packages 顶层条目的「基础名」，配套目录与 xxx-<版本>.dist-info
# 一起被拆走。其余全部留在 exe（白名单式拆分，管理端依赖不会被误伤）。
SPLIT_BASE = {
    # 大头：Playwright 的 Node driver 103MB、numpy 40MB
    "playwright", "numpy", "numpy.libs", "camoufox", "fpgen",
    # 指纹生成 / GeoIP / 浏览器协议
    "language-tags", "maxminddb", "zstandard", "indexed-zstd",
    "greenlet", "pyee", "httpx", "httpcore", "orjson", "screeninfo",
    "ua-parser", "ua-parser-builtins",
    # 注意：opentelemetry 不能拆——fastapi 0.162 启动即 import（fastapi/telemetry），
    # 管理端必须在「运行时包未装」时也能启动；本体仅约 1.5MB，留 exe。
    # Camoufox CLI 及其终端渲染依赖
    "rich", "rich-click", "pygments", "markdown-it", "markdown-it-py", "mdurl",
    "blessed", "jinxed", "ansicon", "inquirer", "editor", "readchar",
    "wcwidth", "xmod", "runs", "yaml", "pyyaml", "peutils", "ordlookup",
}
# 无法用基础名归类的散装文件，精确列举
SPLIT_EXACT = {
    "_yaml",
    "indexed_zstd.cp313-win_amd64.pyd",
    "libzstd-8f07e1112ed283e5cd2798833e9a3c32.dll",
    "msvcp140-a4c2229bdc2a2a630acdc095b4d86008.dll",
}


def _pkg_base(name: str) -> str:
    """把 'language_tags-1.3.2.dist-info' 归一成 'language-tags'。"""
    n = name
    for suffix in (".dist-info", ".egg-info"):
        if n.endswith(suffix):
            n = n[: -len(suffix)]
            break
    if n.endswith((".py", ".pyd", ".dll")):
        n = Path(n).stem
    parts = n.split("-")
    if len(parts) > 1 and parts[-1][:1].isdigit():
        n = "-".join(parts[:-1])
    return n.lower().replace("_", "-")


def _should_split(name: str) -> bool:
    return name in SPLIT_EXACT or _pkg_base(name) in SPLIT_BASE


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

    # 把浏览器运行时拆到独立目录，之后压缩成 runtime-extra-v1.zip。
    # 注意：这一步在 PyInstaller 之前做，exe 里干干净净。
    EXTRA_STAGE.mkdir(parents=True, exist_ok=True)
    moved = 0
    for p in list(site.iterdir()):
        if _should_split(p.name):
            shutil.move(str(p), str(EXTRA_STAGE / p.name))
            moved += 1
    (EXTRA_STAGE / ".pack-version").write_text(RUNTIME_TAG, encoding="utf-8")

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

    return {"runtime_files": n_runtime, "dropped": removed, "app_files": n_app,
            "resources": n_res, "runtime_split": moved,
            "split_mb": round(sum(f.stat().st_size for f in EXTRA_STAGE.rglob("*")
                                  if f.is_file()) / 1024 / 1024, 1)}


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


def build_runtime_pack() -> tuple[Path, Path]:
    """把拆出来的依赖压成 zip + sha256。

    固定条目顺序与时间戳，保证同一份内容在 CI 和本机产出的字节完全一致，
    Release 上的 sha256 才可复验。zip 根就是解压后的 sys.path 目录。
    """
    DIST.mkdir(exist_ok=True)
    # fpgen/data（指纹模型，约1.5MB）不在此列：它不在 PyPI wheel 里，
    # 是运行期由 camoufox.fpgen_model.ensure_fpgen_model() 按需下载的
    # 运行期数据（与内核 / GeoIP 同模式），CI 全新环境本来就没有。
    must = ("playwright/driver/node.exe", "camoufox/utils.py",
            "numpy/__init__.py", "fpgen/__init__.py", ".pack-version")
    for m in must:
        if not (EXTRA_STAGE / m).exists():
            raise RuntimeError(f"运行时包缺少 {m}：拆分清单漏了依赖？")

    files = sorted((p for p in EXTRA_STAGE.rglob("*") if p.is_file()),
                   key=lambda p: p.relative_to(EXTRA_STAGE).as_posix())
    zip_path = DIST / PACK_ZIP
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in files:
            rel = p.relative_to(EXTRA_STAGE).as_posix()
            info = zipfile.ZipInfo(rel, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            z.writestr(info, p.read_bytes())
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    sha_path = DIST / PACK_SHA
    sha_path.write_text(f"{digest}  {PACK_ZIP}\n", encoding="ascii")
    print(f"  运行时包 {zip_path.stat().st_size/1024/1024:.1f} MB / {len(files)} 个文件", flush=True)
    print(f"  SHA256 {digest}", flush=True)
    return zip_path, sha_path


def verify(exe: Path) -> None:
    """列 PKG 内容，确认关键文件在、账号数据与浏览器运行时都不在。"""
    from PyInstaller.archive.readers import CArchiveReader

    reader = CArchiveReader(str(exe))
    names = list(reader.toc.keys())
    must = ["runtime/pythonw.exe", "runtime/python.exe", "app/main.py",
            "app/policy.py", "app/runtime_pack.py", "app/static/index.html",
            "app/static/app.js"]
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

    # 浏览器运行时必须被拆走，否则 exe 又会胖回 86MB
    markers = ("Lib/site-packages/playwright/", "Lib/site-packages/numpy/",
               "Lib/site-packages/camoufox/", "Lib/site-packages/fpgen/",
               "Lib/site-packages/numpy.libs/")
    leaked = [n for n in names if any(m in n.replace("\\", "/") for m in markers)]
    print(f"  浏览器运行时残留: {len(leaked)}")
    if leaked:
        print("   ", leaked[:6])
        raise RuntimeError("浏览器运行时没拆干净，请检查 SPLIT_BASE")

    # opentelemetry 必须留在 exe：fastapi 0.162 启动即 import，
    # 管理端要在「运行时包未装」时就能起来（教训：2026-11-10 拆包首版启动即崩）
    if not any("Lib/site-packages/opentelemetry/" in n.replace("\\", "/") for n in names):
        raise RuntimeError("opentelemetry 被误拆进运行时包，fastapi 将无法启动")

    zip_path = DIST / PACK_ZIP
    with zipfile.ZipFile(zip_path) as z:
        zn = set(z.namelist())
    for m in ("playwright/driver/node.exe", "camoufox/utils.py",
              "numpy/__init__.py", ".pack-version"):
        if m not in zn:
            raise RuntimeError(f"运行时包里缺少 {m}")
    sha_path = DIST / PACK_SHA
    want = sha_path.read_text(encoding="ascii").split()[0]
    got = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    if want != got:
        raise RuntimeError("运行时包 SHA256 与 .sha256 文件不一致")
    print(f"  运行时包 {zip_path.stat().st_size/1024/1024:.1f} MB / {len(zn)} 条目，SHA256 一致")
    print(f"  PKG 条目 {len(names)}，exe {exe.stat().st_size/1024/1024:.1f} MB")


def main() -> int:
    t0 = time.time()
    print("=== 1/4 准备白名单文件并拆分运行时 ===", flush=True)
    info = prepare_stage()
    print(f"  {info}", flush=True)
    print("=== 2/4 PyInstaller 打包（只含管理端） ===", flush=True)
    exe = build_exe()
    print("=== 3/4 生成浏览器运行时包 ===", flush=True)
    zip_path, sha_path = build_runtime_pack()
    print("=== 4/4 校验产物 ===", flush=True)
    verify(exe)
    print(f"\n完成，用时 {time.time()-t0:.0f}s", flush=True)
    for p in (exe, zip_path, sha_path):
        print(f"  {p}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("打包失败:", type(exc).__name__, exc, flush=True)
        sys.exit(1)
