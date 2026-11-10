"""GUI 壳端到端验证：窗口、API、CLI、关闭到托盘、退出清理。"""
from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# 沙箱会注入 HTTP_PROXY；本地回环必须绕过，否则请求会打到代理拿到 502
os.environ["NO_PROXY"] = "127.0.0.1,localhost,::1"
os.environ["no_proxy"] = "127.0.0.1,localhost,::1"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
import common  # noqa: E402
import requests  # noqa: E402

u = ctypes.windll.user32
rt = common.read_runtime()
assert rt, "runtime.json 不存在"
port, token, hwnd, pid = rt["port"], rt["token"], int(rt["hwnd"]), rt["pid"]
B = f"http://127.0.0.1:{port}"
H = {"X-FP-Token": token}
results = []


def chk(name, ok, extra=""):
    results.append(ok)
    print(f"  {'OK  ' if ok else 'FAIL'} {name:32s} {extra}", flush=True)


def alive(p: int) -> bool:
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, p)
    if not h:
        return False
    try:
        c = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(c))
        return c.value == 259
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


print("=== 1. 窗口与服务 ===", flush=True)
chk("主窗口可见", bool(u.IsWindowVisible(hwnd)), f"hwnd={hwnd}")
chk("进程存活", alive(pid), f"pid={pid}")
try:
    r = requests.get(B + "/api/health", timeout=5)
    chk("health 200", r.status_code == 200)
except Exception as e:
    chk("health 200", False, str(e))
try:
    r = requests.get(B + "/api/profiles", headers=H, timeout=5)
    chk("profiles 带 token", r.status_code == 200, f"{len(r.json())} 个环境")
except Exception as e:
    chk("profiles 带 token", False, str(e))
# 回归：确保页面里注入的令牌能真正调通 API（不是只看 runtime.json 直连）
try:
    r = requests.get(B + "/", timeout=5)
    m = re.search(r'window\.__FP_TOKEN__\s*=\s*"([^"]*)"', r.text)
    page_token = m.group(1) if m else ""
    chk("首页注入的令牌一致", page_token == token, f"page={page_token[:8]}...")
    chk("首页禁用缓存", "no-store" in (r.headers.get("cache-control") or ""))
    chk("页面令牌可调 API",
        requests.get(B + "/api/profiles", headers={"X-FP-Token": page_token}, timeout=5).status_code == 200)
except Exception as e:
    chk("页面令牌", False, str(e))

# 真实 WebView2 渲染层检查：抽屉/弹窗是否真被 [hidden] 藏住、页面有无错误提示
try:
    sys.path.insert(0, str(ROOT / "_tools"))
    from dom_probe import probe as dom_probe
    d = dom_probe(B + "/", timeout=45)
    if "error" in d:
        chk("DOM 探测", False, d["error"])
    else:
        chk("页面拿到正确令牌", d.get("token") == token, f"dom={str(d.get('token'))[:8]}...")
        chk("抽屉默认隐藏生效", d.get("drawerHidden") is True, str(d.get("drawerHidden")))
        chk("页面无错误提示", d.get("toastHidden") is True, str(d.get("toastText"))[:40])
        chk("仅一个视图可见", d.get("visibleViews") == 1, str(d.get("visibleViews")))
except Exception as e:
    chk("DOM 探测", False, f"{type(e).__name__}: {e}")

print("=== 1b. 新策略（只读） ===", flush=True)
try:
    s = requests.get(B + "/api/kernel/status", headers=H, timeout=5).json()
    chk("只提示不自动装", s.get("auto_install") is False)
    chk("字库裁剪已停用", s.get("trim_enabled") is False)
    chk("版本/升级时长字段", all(k in s for k in ("installed_at", "age_text", "fonts")),
        s.get("age_text"))
except Exception as e:
    chk("内核策略", False, str(e))
try:
    rows = requests.get(B + "/api/profiles", headers=H, timeout=5).json()
    chk("每行带 network 诊断字段", all(isinstance(p.get("network"), dict) for p in rows),
        f"{len(rows)} 行")
    chk("所有环境 timezone 留空", all(not p.get("timezone") for p in rows))
    chk("config.json 出口数据已回填",
        all((p.get("network") or {}).get("exit_ip") for p in rows),
        ", ".join(str((p.get("network") or {}).get("exit_ip")) for p in rows))
except Exception as e:
    chk("环境诊断字段", False, str(e))

print("=== 2. CLI ===", flush=True)
for cmd, args in [("list", ["list"]), ("status", ["status"]), ("kernel", ["kernel"])]:
    p = subprocess.run(
        [str(ROOT / "runtime" / "python.exe"), str(ROOT / "app" / "main.py"), "--cli", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    try:
        d = json.loads(p.stdout)
        chk(f"CLI {cmd}", p.returncode == 0 and isinstance(d, (dict, list)), str(d)[:60])
    except Exception:
        chk(f"CLI {cmd}", False, (p.stdout + p.stderr)[:120])

print("=== 3. 单实例（重复启动应直接退出） ===", flush=True)
t0 = time.time()
p = subprocess.Popen([str(ROOT / "runtime" / "pythonw.exe"), str(ROOT / "app" / "main.py")])
try:
    p.wait(timeout=12)
    exited = True
except subprocess.TimeoutExpired:
    exited = False
    p.kill()
    p.wait(timeout=5)
chk("重复启动自行退出", exited, f"{time.time()-t0:.1f}s")
chk("主窗口仍在", bool(u.IsWindowVisible(hwnd)))

print("=== 4. 点 X 隐藏到托盘 ===", flush=True)
u.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
time.sleep(2.5)
chk("窗口已隐藏", not bool(u.IsWindowVisible(hwnd)))
chk("服务仍在（托盘驻留）", alive(pid))
try:
    chk("隐藏后 API 仍可访问", requests.get(B + "/api/health", timeout=5).status_code == 200)
except Exception as e:
    chk("隐藏后 API 仍可访问", False, str(e))
u.ShowWindow(hwnd, 9)
u.SetForegroundWindow(hwnd)
time.sleep(0.8)
chk("托盘恢复窗口", bool(u.IsWindowVisible(hwnd)))

print("=== 5. 退出清理 ===", flush=True)
try:
    r = requests.post(B + "/api/shutdown", headers=H, timeout=5)
    chk("shutdown 返回", r.status_code == 200)
except Exception as e:
    chk("shutdown 返回", False, str(e))
for _ in range(20):
    if not alive(pid):
        break
    time.sleep(0.4)
chk("进程已退出", not alive(pid))
chk("runtime.json 已清理", not common.RUNTIME_JSON.exists())

print(f"\n==== {sum(results)}/{len(results)} 通过 ====", flush=True)
sys.exit(0 if all(results) else 1)
