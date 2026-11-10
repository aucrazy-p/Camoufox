"""
公共路径、设置与服务发现。

两种运行模式：
  开发模式   直接 python app/main.py，数据仍在 D:\\Code\\Camoufox\\data，
             复用现有两个环境与已裁剪内核，方便本机调试。
  打包模式   FP_FROZEN=1，**便携版**：数据放 exe 所在目录的 CamoufoxData\\，
             exe 放哪数据就跟到哪，不写 C 盘；拷贝整个文件夹即可换电脑。
             FP_EXE_PATH 由 boot.py 传入（子进程 sys.executable 是内嵌 pythonw，
             不是 exe 本身，不能用它定位）。

重要：这里**不能**改全局 os.environ["LOCALAPPDATA"]。
上一版为了把 Camoufox 的缓存收进项目目录改了它，结果 webbrowser.open 拉起的
Edge 也跟着把 619MB 用户配置/扩展全写进了项目。现在只接管 platformdirs 对
"camoufox" 这一个 appname 的解析，其它程序一律用系统真实路径。
"""

from __future__ import annotations

import json
import os
import platform
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parent              # .../app
ROOT = BASE.parent                                  # 程序根
IS_FROZEN = os.environ.get("FP_FROZEN") == "1"

APP_NAME = "CamoufoxManager"
VERSION = "1.0.0"


def _default_data_root() -> Path:
    """打包版 = 真便携：数据根 = exe 所在目录 / CamoufoxData。

    绝不回退到 %LOCALAPPDATA%：用户要求所有东西跟 exe 走、不往 C 盘放。
    若目录不可写，boot.py 会在启动子进程前弹窗报错退出，这里不静默改道。
    """
    if IS_FROZEN:
        exe = os.environ.get("FP_EXE_PATH")
        if exe:
            return Path(exe).resolve().parent / "CamoufoxData"
        # 极端兜底：未经 boot.py 启动时，_MEIPASS 的父目录就是 exe 所在目录
        app_dir = os.environ.get("FP_APP_DIR")
        if app_dir:
            return Path(app_dir).resolve().parent / "CamoufoxData"
        raise RuntimeError("便携版无法确定 exe 位置（缺少 FP_EXE_PATH）")
    return ROOT / "data"


# 允许用 FP_DATA_DIR 覆盖，方便从干净目录做端到端验证；boot.py 也用它指定便携目录
DATA = Path(os.environ.get("FP_DATA_DIR") or _default_data_root())
PROFILES = DATA / "profiles"
KERNEL_ROOT = DATA / "kernel"
TRASH = DATA / "trash"
LOGS = DATA / "logs"
DOWNLOADS = DATA / "downloads"

# platformdirs 解析 "camoufox" 时的落点（GeoIP / fpgen 缓存）
APP_DATA = DATA / "appdata"
KERNEL_DIR = APP_DATA / "camoufox" / "Cache"

DB_PATH = DATA / "app.db"
SETTINGS_PATH = DATA / "settings.json"
RUNTIME_JSON = DATA / "runtime.json"

RESOURCES = ROOT / "resources"          # 字体黑名单、版本清单等只读资源
ASSETS = BASE / "assets"                # 图标

DEFAULT_SETTINGS = {
    "theme": "dark",                 # dark / light / system
    "close_action": "tray",          # tray / exit
    "autostart": False,
    "download_proxy": "",            # 留空=直连，可填 http://host:port
    "kernel_pin": "",                # 锁定内核版本，空=不自动升级
}

_prepared = False
_settings_lock = threading.Lock()


# ---------------------------------------------------------------- 环境准备

def _redirect_platformdirs() -> None:
    """只把 Camoufox 自己的目录解析接到数据目录，不影响其它程序。"""
    try:
        import platformdirs
    except Exception:
        return
    if getattr(platformdirs, "_fp_redirected", False):
        return

    cache_target = KERNEL_DIR
    data_target = APP_DATA / "camoufox" / "Data"
    orig_cache = platformdirs.user_cache_dir
    orig_data = platformdirs.user_data_dir

    def _is_ours(appname) -> bool:
        return bool(appname) and str(appname).strip().lower() == "camoufox"

    def user_cache_dir(appname=None, *args, **kwargs):
        if _is_ours(appname):
            cache_target.mkdir(parents=True, exist_ok=True)
            return str(cache_target)
        return orig_cache(appname, *args, **kwargs)

    def user_data_dir(appname=None, *args, **kwargs):
        if _is_ours(appname):
            data_target.mkdir(parents=True, exist_ok=True)
            return str(data_target)
        return orig_data(appname, *args, **kwargs)

    platformdirs.user_cache_dir = user_cache_dir
    platformdirs.user_data_dir = user_data_dir
    platformdirs._fp_redirected = True


def prepare_env() -> None:
    """创建数据目录并接管 Camoufox 路径解析。幂等，绝不改 LOCALAPPDATA。"""
    global _prepared
    for d in (DATA, PROFILES, KERNEL_ROOT, TRASH, LOGS, DOWNLOADS, APP_DATA,
              DATA / "webview2", DATA / "tmp"):
        d.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    _redirect_platformdirs()
    _prepared = True


# ---------------------------------------------------------------- 设置

def load_settings() -> dict:
    data = dict(DEFAULT_SETTINGS)
    try:
        data.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        pass
    return data


def save_settings(patch: dict) -> dict:
    allowed = set(DEFAULT_SETTINGS)
    with _settings_lock:
        cur = load_settings()
        cur.update({k: v for k, v in (patch or {}).items() if k in allowed})
        tmp = SETTINGS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(SETTINGS_PATH)
        return cur


def write_runtime(port: int, token: str, pid: int, hwnd: int = 0) -> None:
    payload = {
        "app": APP_NAME,
        "version": VERSION,
        "port": port,
        "token": token,
        "pid": pid,
        "hwnd": hwnd,
        "host": "127.0.0.1",
        "data_dir": str(DATA),
    }
    tmp = RUNTIME_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(RUNTIME_JSON)


def read_runtime() -> dict | None:
    try:
        return json.loads(RUNTIME_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def clear_runtime() -> None:
    RUNTIME_JSON.unlink(missing_ok=True)


def log_file(name: str) -> Path:
    LOGS.mkdir(parents=True, exist_ok=True)
    return LOGS / name


# ---------------------------------------------------------------- 内核

def kernel_ready() -> bool:
    return kernel_exe() is not None


def kernel_exe() -> Path | None:
    """内核可执行文件：data/kernel/<版本目录>/camoufox.exe，多个版本取名字最大的。"""
    if not KERNEL_ROOT.exists():
        return None
    found = [p for p in KERNEL_ROOT.glob("*/camoufox.exe") if p.is_file()]
    if not found:
        return None
    return sorted(found, key=lambda p: p.parent.name)[-1]


def kernel_version() -> str:
    exe = kernel_exe()
    return exe.parent.name if exe else ""


def work_area() -> tuple[int, int]:
    """桌面可用区域（扣掉任务栏）的宽高。"""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            rect = wintypes.RECT()
            ok = ctypes.windll.user32.SystemParametersInfoW(
                0x0030, 0, ctypes.byref(rect), 0  # SPI_GETWORKAREA
            )
            if ok:
                return rect.right - rect.left, rect.bottom - rect.top
        except Exception:
            pass
    return real_screen()


def fonts_dir() -> Path | None:
    exe = kernel_exe()
    if exe is None:
        return None
    d = exe.parent / "fonts"
    return d if d.is_dir() else None


def kernel_windows_only() -> bool:
    """这份内核是不是旧版裁剪过的（只剩 Windows 一套字库）。

    取消裁剪后新装的内核不会再中这个判断；仅用于兼容开发机上那份
    已经裁过的 v158.0.1-beta.41，避免放开 macOS/Linux 却渲染不出字体。
    """
    exe = kernel_exe()
    if exe is None:
        return False
    if (exe.parent / "TRIM_WINDOWS_ONLY.txt").exists():
        return True
    d = fonts_dir()
    if d is None:
        return False
    try:
        return sum(1 for _ in d.iterdir()) < 800
    except OSError:
        return False


def supported_oses() -> list[str]:
    """按字库实际能力决定可选系统，不做产品层锁定。

    不裁剪后新装的内核带完整三套字库，三种身份都可用；
    只有历史遗留的裁剪内核才限制成 Windows。
    内核还没装时也放行三系统 —— 装完本来就是完整 bundle。
    """
    if kernel_windows_only():
        return ["windows"]
    return ["windows", "macos", "linux"]


def fpgen_ready() -> bool:
    """指纹生成模型是否就位。打包后模型在 runtime 内，通常恒为 True。"""
    try:
        import importlib.util

        spec = importlib.util.find_spec("fpgen")
        if spec is None or spec.origin is None:
            return False
        data = Path(spec.origin).parent / "data"
        return data.exists() and any(data.iterdir())
    except Exception:
        return False


def geoip_ready() -> bool:
    try:
        d = KERNEL_DIR / "geoip" / "mmdb"
        return d.exists() and any(d.glob("*.mmdb"))
    except Exception:
        return False


def real_screen() -> tuple[int, int]:
    """本机真实分辨率。内嵌 Python 没有 tkinter，Windows 上直接用 API 取。"""
    if os.name == "nt":
        try:
            import ctypes

            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                try:
                    ctypes.windll.user32.SetProcessDPIAware()
                except Exception:
                    pass
            u = ctypes.windll.user32
            w, h = int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))
            if w > 0 and h > 0:
                return w, h
        except Exception:
            pass
        return 1920, 1080

    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        size = (root.winfo_screenwidth(), root.winfo_screenheight())
        root.destroy()
        return size
    except Exception:
        return 1920, 1080


# ---------------------------------------------------------------- 机器标识

def video_adapters() -> list[str]:
    """注册表里读到的显卡名，用来识别「换了显卡 / 换了机器」。"""
    if os.name != "nt":
        return []
    names: list[str] = []
    try:
        import winreg

        base = (
            r"SYSTEM\CurrentControlSet\Control\Class"
            r"\{4d36e968-e325-11ce-bfc1-08002be10318}"
        )
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
        try:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                if not sub.isdigit():
                    continue
                try:
                    sk = winreg.OpenKey(key, sub)
                    try:
                        val, _ = winreg.QueryValueEx(sk, "DriverDesc")
                        if val:
                            names.append(str(val))
                    finally:
                        winreg.CloseKey(sk)
                except OSError:
                    continue
        finally:
            winreg.CloseKey(key)
    except OSError:
        return []
    return sorted(set(names))


def machine_id() -> str:
    """本机标识，用于让显卡探测缓存在换机器 / 换显卡后自动失效。

    host_gpu 决定 Canvas 读回是否随机化、WebGPU 是否暴露真实适配器，
    缓存错等于直接漏真实显卡，所以必须绑定机器，绝不能随程序分发。
    """
    parts = [
        platform.node(),
        os.environ.get("PROCESSOR_IDENTIFIER", ""),
        platform.processor(),
        "x".join(map(str, real_screen())),
        *video_adapters(),
    ]
    return "|".join(p for p in parts if p)


# ---------------------------------------------------------------- 已停用：字体裁剪清单
#
# 早期按 fonts_not_win.txt / fonts_win.txt 的文件名黑白名单删掉 850 个
# macOS/Linux 字体。官方 font-bundle 是独立发版的构建资源（v1=1513 files，
# v4=1532 files，41 天内迭代 4 版，还会改「哪些字体服务哪个 OS 身份」），
# 按文件名维护清单跨版本必然失配、会中断内核升级。已改为保留完整字库，
# 详见 policy.py。resources/ 下两份 txt 仅作历史记录，不再被代码引用。
