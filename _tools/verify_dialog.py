"""端到端验证 choose_zip：真实 pywebview 环境里弹原生文件对话框。

背景（2026-11-10 实锤 bug）：pywebview 5.0 起模块级 webview.create_file_dialog
被移除，老写法 AttributeError 被 _fatal 吞进日志，前端表现为「点了没反应」。
修复后 choose_zip 用 Window.create_file_dialog。

本脚本证明两件事：
  1. choose_zip 在 js_api 场景下真的能弹出原生 #32770 对话框；
  2. 对话框被关闭后 choose_zip 干净返回 None（无异常）。

用法： runtime\\python.exe _tools\\verify_dialog.py
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_CHILD = r'''
import ctypes
from ctypes import wintypes
import json, sys, threading, time
sys.path.insert(0, sys.argv[1])            # 项目 app/ 目录
os_dir = sys.argv[1]
import webview
import main                                # 导入即可，不会执行 __main__

u = ctypes.windll.user32
found = {"hwnd": 0}

def close_dialog_later():
    """轮询等原生对话框出现，出现后立刻关掉，让 choose_zip 返回 None。"""
    for _ in range(80):                    # 最多 8 秒
        hwnd = _find_new_dialog()
        if hwnd:
            found["hwnd"] = hwnd
            time.sleep(0.3)
            u.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE
            return
        time.sleep(0.1)

def _find_new_dialog():
    hits = []
    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, l):
        if u.IsWindowVisible(h):
            buf = ctypes.create_unicode_buffer(16)
            u.GetClassNameW(h, buf, 16)
            if buf.value == "#32770":
                hits.append(h)
        return True
    u.EnumWindows(cb, 0)
    return hits[0] if hits else 0

class Api:
    def pick(self):
        threading.Thread(target=close_dialog_later, daemon=True).start()
        t0 = time.time()
        try:
            r = main.choose_zip()
            d = {"ret": r, "elapsed": round(time.time()-t0, 1),
                 "dialog_seen": bool(found["hwnd"])}
        except Exception as e:
            d = {"error": f"{type(e).__name__}: {e}",
                 "dialog_seen": bool(found["hwnd"])}
        print("__FP_DIALOG__" + json.dumps(d), flush=True)
        try:
            w.destroy()                # 结果已上报，关窗让子进程干净退出
        except Exception:
            pass
        return json.dumps(d)

w = webview.create_window("dialog-probe", "https://example.com",
                          width=320, height=200, js_api=Api())
main._STATE["win"] = w
# 窗口起来后自动触发一次 pick
def _auto():
    time.sleep(1.5)
    try:
        w.evaluate_js("pywebview.api.pick()")
    except Exception as e:
        print("__FP_DIALOG__" + json.dumps({"error": f"JS 调用失败: {e}"}),
              flush=True)
threading.Thread(target=_auto, daemon=True).start()
webview.start(gui="edgechromium", private_mode=True)
'''


def find_dialog_windows() -> list[int]:
    u = ctypes.windll.user32
    hits: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, l):
        if u.IsWindowVisible(h):
            buf = ctypes.create_unicode_buffer(16)
            u.GetClassNameW(h, buf, 16)
            if buf.value == "#32770":
                hits.append(h)
        return True

    u.EnumWindows(cb, 0)
    return hits


def main() -> int:
    proc = subprocess.Popen(
        [str(ROOT / "runtime" / "python.exe"), "-c", _CHILD,
         str(ROOT / "app")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text="utf-8",
        errors="replace", cwd=str(ROOT),
    )
    ok, out = False, ""
    try:
        out = proc.communicate(timeout=40)[0]
    except subprocess.TimeoutExpired:
        proc.kill()
        out = proc.communicate()[0]
        print("FAIL 子进程超时（对话框可能弹出后没人关）")

    # 子进程用 print(json) 不方便截取，改为扫关键字
    print("---- 子进程输出 ----")
    print(out.strip()[-800:])
    print("--------------------")
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("__FP_DIALOG__"):
            try:
                d = json.loads(line[len("__FP_DIALOG__"):])
            except ValueError:
                continue
            ok = bool(d.get("dialog_seen")) and "error" not in d and d.get("ret") is None
            print(f"{'OK  ' if ok else 'FAIL'} 对话框弹出并被关闭 "
                  f"(seen={d.get('dialog_seen')} ret={d.get('ret')!r} "
                  f"err={d.get('error')})")
            break

    # 双保险：如果子进程还活着且屏幕上残留对话框，清掉
    leftover = find_dialog_windows()
    u = ctypes.windll.user32
    for h in leftover:
        u.PostMessageW(h, 0x0010, 0, 0)
    if leftover:
        print(f"NOTE 清理残留对话框 {len(leftover)} 个")

    print("==== 全过 ====" if ok else "==== 未过 ====")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
