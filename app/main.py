"""
应用程序入口：GUI 窗口 + 系统托盘 + 本地服务 + CLI。

  pythonw main.py              正常启动（无控制台窗口）
  python  main.py --cli list   CLI 模式，输出 JSON 供自动化调用

窗口控制全部用 Win32 API 操作 hwnd，不跨线程调 WinForms 控件：
js_api 回调和托盘菜单都不在 UI 线程，直接碰 .native 会抛异常。
"""

from __future__ import annotations

import ctypes
import json
import os
import secrets
import socket
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402


class _NullIO:
    """pythonw.exe 下 sys.stdout / sys.stderr 是 None。

    uvicorn 的 DefaultFormatter 会调 sys.stdout.isatty()，Third-party 库也常直接
    print。给一个安静的替身，既不弹窗也不丢异常；真正的错误由 _fatal 写日志。
    """

    encoding = "utf-8"
    errors = "replace"

    def isatty(self): return False
    def write(self, s): return len(s) if s else 0
    def writelines(self, lines): return None
    def flush(self): return None
    def fileno(self): raise OSError("无控制台")


if sys.stdout is None:
    sys.stdout = _NullIO()
if sys.stderr is None:
    sys.stderr = _NullIO()

# 模块级状态：js_api 只暴露纯函数，绝不把 Window 对象挂在可序列化的对象上。
# 之前把 window 存进 Api 实例属性，pywebview 反射时把 WinForms 对象无限展开刷屏。
_STATE = {"win": None, "icon": None, "hwnd": 0, "server": None, "port": 0, "token": ""}
FORCE_EXIT = False
MUTEX_HANDLE = None


# ---------------------------------------------------------------- 日志兜底

def _fatal(exc: BaseException) -> None:
    try:
        with common.log_file("error.log").open("a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except Exception:
        pass


def _excepthook(t, v, tb) -> None:
    _fatal(v)


# ---------------------------------------------------------------- 单实例

def single_instance() -> bool:
    """返回 True 表示「我是第一个实例，可以继续」。"""
    global MUTEX_HANDLE
    if os.name != "nt":
        return True
    k = ctypes.windll.kernel32
    k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    k.CreateMutexW.restype = ctypes.c_void_p
    MUTEX_HANDLE = k.CreateMutexW(None, False, "CamoufoxManager_SingleInstance_v1")
    if k.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        rt = common.read_runtime()
        hwnd = int(rt.get("hwnd") or 0) if rt else 0
        if hwnd:
            u = ctypes.windll.user32
            u.ShowWindow(hwnd, 9)       # SW_RESTORE
            u.SetForegroundWindow(hwnd)
        return False
    return True


# ---------------------------------------------------------------- 端口 / 服务

def pick_port(start: int = 8760) -> int:
    for p in range(start, start + 60):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError("8760-8819 端口全部被占用")


def wait_port(port: int, timeout: float = 20.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def start_server(port: int, token: str):
    os.environ["FP_PORT"] = str(port)
    os.environ["FP_TOKEN"] = token
    import uvicorn
    import app as api_module

    cfg = uvicorn.Config(
        api_module.app, host="127.0.0.1", port=port,
        log_level="warning", access_log=False,
        log_config=None,  # pythonw 没有控制台，别让 dictConfig 去碰 stdout
    )
    server = uvicorn.Server(cfg)
    api_module.set_shutdown_callback(request_quit)
    threading.Thread(target=server.run, daemon=True, name="uvicorn").start()
    return server


# ---------------------------------------------------------------- Win32 窗口操作

def _hwnd() -> int:
    return int(_STATE.get("hwnd") or 0)


def _w32(action: str) -> None:
    h = _hwnd()
    if not h or os.name != "nt":
        return
    u = ctypes.windll.user32
    if action == "minimize":
        u.ShowWindow(h, 6)
    elif action == "maximize":
        u.ShowWindow(h, 3 if not u.IsZoomed(h) else 9)
    elif action == "show":
        u.ShowWindow(h, 9)
        u.SetForegroundWindow(h)
    elif action == "hide":
        u.ShowWindow(h, 0)
    elif action in ("close", "quit"):
        u.PostMessageW(h, 0x0010, 0, 0)  # WM_CLOSE，交给 UI 线程的 closing 事件


# ---------------------------------------------------------------- js_api 纯函数

def window_action(action: str) -> str:
    """前端标题栏按钮调用。close 统一发 WM_CLOSE，由 on_closing 决定托盘还是退出。"""
    if action in ("minimize", "maximize", "close"):
        _w32(action)
    return "ok"


def choose_zip():
    """选本地内核 zip，返回路径字符串（或 None）。"""
    try:
        import webview
        r = webview.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False,
            file_types=("ZIP 压缩包 (*.zip)",),
        )
        return r[0] if r else None
    except Exception as exc:
        _fatal(exc)
        return None


def apply_autostart(enabled: bool) -> str:
    """写 HKCU Run 项。不需要管理员权限。"""
    if os.name != "nt":
        return "unsupported"
    exe = os.environ.get("FP_EXE_PATH") or sys.argv[0]
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        )
        try:
            if enabled:
                winreg.SetValueEx(key, "指纹环境管理器", 0, winreg.REG_SZ, f'"{exe}"')
            else:
                try:
                    winreg.DeleteValue(key, "指纹环境管理器")
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(key)
        return "ok"
    except Exception as exc:
        _fatal(exc)
        return f"error: {exc}"


# ---------------------------------------------------------------- 托盘

def _tray_image():
    from PIL import Image
    p = common.ASSETS / "icon.png"
    if p.exists():
        return Image.open(p).convert("RGBA")
    img = Image.new("RGBA", (64, 64), (20, 22, 26, 255))
    return img


def start_tray() -> None:
    import pystray

    def on_show(icon, item):
        _w32("show")

    def on_quit(icon, item):
        global FORCE_EXIT
        FORCE_EXIT = True
        try:
            icon.stop()
        except Exception:
            pass
        _w32("quit")

    menu = pystray.Menu(
        pystray.MenuItem("显示主界面", on_show, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", on_quit),
    )
    icon = pystray.Icon("CamoufoxManager", _tray_image(), "指纹环境管理器", menu)
    _STATE["icon"] = icon
    threading.Thread(target=icon.run, daemon=True, name="tray").start()


def request_quit() -> None:
    """托盘退出 / POST /api/shutdown 都走这里。"""
    global FORCE_EXIT
    FORCE_EXIT = True
    try:
        if _STATE["icon"]:
            _STATE["icon"].stop()
    except Exception:
        pass
    _w32("quit")


# ---------------------------------------------------------------- GUI

def grab_hwnd_loop() -> None:
    """窗口创建后取 hwnd 写进 runtime.json，供第二个实例唤起。"""
    for _ in range(80):
        win = _STATE.get("win")
        if win is not None:
            try:
                native = getattr(win, "native", None)
                if native is not None:
                    h = int(native.Handle.ToInt64())
                    if h:
                        _STATE["hwnd"] = h
                        common.write_runtime(
                            _STATE["port"], _STATE["token"], os.getpid(), h
                        )
                        return
            except Exception:
                pass
        time.sleep(0.25)


def on_closing():
    """返回 False = 取消关闭（缩到托盘）；返回 True = 真的退出。"""
    global FORCE_EXIT
    if FORCE_EXIT:
        return True
    try:
        if common.load_settings().get("close_action") == "exit":
            FORCE_EXIT = True
            return True
    except Exception:
        pass
    _w32("hide")
    try:
        if _STATE["icon"]:
            _STATE["icon"].notify("已缩到系统托盘，点托盘图标可重新打开", "指纹环境管理器")
    except Exception:
        pass
    return False


def run_gui(port: int, token: str) -> None:
    import webview

    win = webview.create_window(
        "指纹环境管理器",
        url=f"http://127.0.0.1:{port}/",
        width=1280, height=800, min_size=(1000, 620),
        frameless=True, easy_drag=True,
        background_color="#0E0F11",
    )
    _STATE["win"] = win
    win.expose(window_action, choose_zip, apply_autostart)
    win.events.closing += on_closing
    threading.Thread(target=grab_hwnd_loop, daemon=True, name="hwnd").start()
    # 便携版：WebView2 缓存也放数据目录，不用系统 %TEMP% / %APPDATA% 的临时目录
    webview.start(gui="edgechromium", debug=False, private_mode=False,
                  storage_path=str(common.DATA / "webview2"))


# ---------------------------------------------------------------- CLI

def run_cli(argv: list[str]) -> int:
    rt = common.read_runtime()
    if not rt:
        print(json.dumps({"ok": False, "error": "服务未运行，请先启动应用程序"}, ensure_ascii=False))
        return 1
    base = f"http://{rt.get('host', '127.0.0.1')}:{rt.get('port')}"
    headers = {"X-FP-Token": rt.get("token", "")}
    import requests

    cmd = (argv[0] if argv else "status").lower()
    pid = argv[1] if len(argv) > 1 else ""
    try:
        if cmd in ("list", "profiles"):
            r = requests.get(base + "/api/profiles", headers=headers, timeout=10)
        elif cmd in ("start", "stop"):
            if not pid:
                raise ValueError(f"{cmd} 需要环境 id")
            r = requests.post(f"{base}/api/profiles/{pid}/{cmd}", headers=headers, timeout=30)
        elif cmd == "status":
            r = requests.get(base + "/api/health", headers=headers, timeout=10)
        elif cmd == "kernel":
            r = requests.get(base + "/api/kernel/status", headers=headers, timeout=10)
        else:
            raise ValueError(f"未知命令 {cmd}（可用：list / status / kernel / start / stop）")
        print(json.dumps(r.json(), ensure_ascii=False, indent=2))
        return 0 if r.ok else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1


# ---------------------------------------------------------------- 主流程

def main() -> int:
    sys.excepthook = _excepthook
    common.prepare_env()

    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        return run_cli(sys.argv[2:])

    if not single_instance():
        return 0  # 已有实例，已把它的窗口唤到前台

    port = pick_port()
    token = secrets.token_urlsafe(24)
    _STATE["port"], _STATE["token"] = port, token
    common.write_runtime(port, token, os.getpid())

    server = start_server(port, token)
    _STATE["server"] = server
    if not wait_port(port):
        raise RuntimeError("本地服务未在 20 秒内就绪")

    start_tray()
    try:
        run_gui(port, token)
    finally:
        try:
            server.should_exit = True
        except Exception:
            pass
        try:
            if _STATE["icon"]:
                _STATE["icon"].stop()
        except Exception:
            pass
        common.clear_runtime()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as exc:
        _fatal(exc)
        code = 1
    # os._exit 不会刷新 stdio 缓冲，CLI 的 JSON 输出会整段丢失。
    # 这里显式刷一次；boot.py 里另外设了 PYTHONUNBUFFERED=1 做双保险。
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    # uvicorn / pystray 都是 daemon 线程，这里强制收尾，避免进程残留
    os._exit(code if isinstance(code, int) else 0)
