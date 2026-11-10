"""从干净数据目录验证打包版 exe，模拟「拷到另一台电脑首次运行」。

用法： runtime\\python.exe _tools\\verify_exe.py
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "_tools"
EXE = ROOT / "dist" / "指纹环境管理器.exe"
FRESH = TOOLS / "fresh_data"
u = ctypes.windll.user32
results = []


def chk(name, ok, extra=""):
    results.append(bool(ok))
    print(f"  {'OK  ' if ok else 'FAIL'} {name:34s} {extra}", flush=True)
    return ok


def main() -> int:
    if not EXE.exists():
        print("exe 不存在，请先运行 build.py")
        return 1
    shutil.rmtree(FRESH, ignore_errors=True)
    FRESH.mkdir(parents=True)

    env = os.environ.copy()
    env["FP_DATA_DIR"] = str(FRESH)
    env["FP_FROZEN"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    # 沙箱会注入 HTTP_PROXY；本地回环必须绕过，否则请求会打到代理拿到 502
    env["NO_PROXY"] = "127.0.0.1,localhost,::1"
    env["no_proxy"] = "127.0.0.1,localhost,::1"

    print(f"=== 启动 {EXE.name}（{EXE.stat().st_size/1024/1024:.1f} MB） ===", flush=True)
    t0 = time.time()
    proc = subprocess.Popen([str(EXE)], env=env, cwd=str(ROOT))
    rt_file = FRESH / "runtime.json"
    rt = None
    for _ in range(180):
        if rt_file.exists():
            try:
                cand = json.loads(rt_file.read_text(encoding="utf-8"))
                if cand.get("hwnd"):
                    rt = cand
                    break
            except (ValueError, OSError):
                pass
        if proc.poll() is not None:
            print("exe 提前退出，退出码", proc.returncode)
            break
        time.sleep(0.5)
    boot = time.time() - t0
    ready = chk("exe 启动就绪（窗口已创建）", rt is not None, f"耗时 {boot:.1f}s")

    if not ready:
        print("错误日志：")
        p = FRESH / "logs" / "error.log"
        print(p.read_text(encoding="utf-8", errors="replace")[-2000:] if p.exists() else "(无)")
        return 1
    B = f"http://127.0.0.1:{rt['port']}"
    H = {"X-FP-Token": rt["token"]}
    import requests

    print("=== 窗口与数据隔离 ===", flush=True)
    hwnd = int(rt["hwnd"])
    chk("主窗口可见", bool(u.IsWindowVisible(hwnd)), f"hwnd={hwnd}")
    chk("数据落在独立目录",
        Path(rt["data_dir"]).resolve() == FRESH.resolve(), rt["data_dir"])
    chk("与项目 data 隔离", Path(rt["data_dir"]).resolve() != (ROOT / "data").resolve())

    # 回归：早期版本把 __FP_TOKEN__ 占位符和 JS 变量写成同名，str.replace 连变量名
    # 一起替换，页面拿到 undefined，所有 /api 全 401，但直连 API 的用例全过。
    print("=== 页面令牌（GUI 实际拿到的令牌） ===", flush=True)
    try:
        r = requests.get(B + "/", timeout=10)
        m = re.search(r'window\.__FP_TOKEN__\s*=\s*"([^"]*)"', r.text)
        page_token = m.group(1) if m else ""
        chk("首页 200 且注入令牌", r.status_code == 200 and page_token == rt["token"],
            f"page={page_token[:8]}... runtime={rt['token'][:8]}...")
        chk("首页禁用缓存", "no-store" in (r.headers.get("cache-control") or ""))
        chk("页面令牌可调 API",
            requests.get(B + "/api/profiles", headers={"X-FP-Token": page_token}, timeout=10).status_code == 200)
    except Exception as e:
        chk("页面令牌", False, f"{type(e).__name__}: {e}")

    # 真实 WebView2 渲染层检查：抽屉/弹窗是否真被 [hidden] 藏住、页面有无错误提示
    print("=== 页面 DOM（真实 WebView2 渲染） ===", flush=True)
    try:
        sys.path.insert(0, str(TOOLS))
        from dom_probe import probe as dom_probe
        d = dom_probe(B + "/", timeout=45)
        if "error" in d:
            chk("DOM 探测", False, d["error"])
        else:
            chk("页面拿到正确令牌", d.get("token") == rt["token"], f"dom={str(d.get('token'))[:8]}...")
            chk("抽屉默认隐藏生效", d.get("drawerHidden") is True, str(d.get("drawerHidden")))
            chk("页面无错误提示", d.get("toastHidden") is True, str(d.get("toastText"))[:40])
            chk("仅一个视图可见", d.get("visibleViews") == 1, str(d.get("visibleViews")))
            chk("语言控件是下拉", d.get("localeTag") == "SELECT", str(d.get("localeTag")))
            chk("含 en-US/zh-CN/ja-JP/自定义",
                all(d.get(k) is True for k in
                    ("localeHasEnUS", "localeHasZhCN", "localeHasJaJP", "localeHasCustom")),
                f"选项={d.get('localeOptionCount')} 分组={d.get('localeGroups')}")
            chk("自定义框默认隐藏", d.get("localeCustomHiddenBefore") is True,
                str(d.get("localeCustomHiddenBefore")))
            chk("自定义切换显隐生效", d.get("localeToggle") is True, str(d.get("localeToggle")))
            chk("下拉/自定义提交取值正确", d.get("localePayload") is True, str(d.get("localePayload")))
    except Exception as e:
        chk("DOM 探测", False, f"{type(e).__name__}: {e}")

    print("=== 配置签名含语言（改语言必须重算配置） ===", flush=True)
    try:
        sys.path.insert(0, str(ROOT / "app"))
        import runner as _runner
        base = {"proxy_scheme": "socks5", "proxy_host": "127.0.0.1",
                "proxy_port": "3067", "locale": "en-US"}
        a = _runner.config_signature(base)
        b = _runner.config_signature(dict(base, locale="zh-CN"))
        c = _runner.config_signature(dict(base, proxy_host="10.0.0.1"))
        chk("语言变了签名变", a != b, f"{a} -> {b}")
        chk("代理变了签名变", a != c, c)
        chk("网络签名仍是三段", len(_runner.proxy_signature(base).split("|")) == 3,
            _runner.proxy_signature(base))
        chk("新签名保留代理前缀", a.startswith(_runner.proxy_signature(base) + "|"), a)
    except Exception as e:
        chk("配置签名", False, f"{type(e).__name__}: {e}")

    print("=== 首次运行：内核缺失引导 ===", flush=True)
    try:
        h = requests.get(B + "/api/health", timeout=10).json()
        chk("内核状态=未安装", not h["camoufox"]["ready"] or not h["camoufox"]["parts"].get("浏览器内核"),
            h["camoufox"]["reason"][:60])
    except Exception as e:
        chk("health", False, str(e))
    try:
        s = requests.get(B + "/api/kernel/status", headers=H, timeout=10).json()
        chk("kernel/status 未安装", not s["installed"], f"version={s['version']!r}")
    except Exception as e:
        chk("kernel/status", False, str(e))

    print("=== 浏览器运行时包（按需安装，用本地 zip 离线装） ===", flush=True)
    try:
        ZIP = ROOT / "dist" / "runtime-extra-v1.zip"
        s = requests.get(B + "/api/runtime/status", headers=H, timeout=10).json()
        chk("运行时包初始未安装", s["installed"] is False, str(s.get("modules")))
        chk("固定 tag 与下载直链",
            s["tag"] == "runtime-v1"
            and s["url"] == "https://github.com/aucrazy-p/Camoufox/releases/download/runtime-v1/runtime-extra-v1.zip",
            s["url"])
        chk("查状态绝不自动下载", s["auto_install"] is False, str(s["auto_install"]))
        h = requests.get(B + "/api/health", timeout=10).json()["camoufox"]
        chk("health 提示先装运行时",
            h["parts"]["浏览器运行时"] is False and "运行时未安装" in h["reason"], h["reason"][:50])

        r = requests.post(B + "/api/runtime/install", headers=H,
                          json={"local_zip": str(ZIP)}, timeout=10)
        chk("发起本地 zip 安装", r.status_code == 200 and r.json().get("ok") is True,
            f"status={r.status_code}")
        phase = ""
        for _ in range(180):
            p = requests.get(B + "/api/runtime/progress", headers=H, timeout=10).json()
            phase = p.get("phase", "")
            if phase in ("done", "error", "idle"):
                break
            time.sleep(1)
        chk("安装完成", phase == "done",
            requests.get(B + "/api/runtime/progress", headers=H, timeout=10).json().get("error", phase))

        s2 = requests.get(B + "/api/runtime/status", headers=H, timeout=10).json()
        chk("装后状态=已安装", s2["installed"] is True, f"size={s2.get('size_mb')}MB")
        chk("关键模块全部就位", all(s2["modules"].values()),
            ",".join(k for k, v in s2["modules"].items() if not v))
        chk("版本标记匹配", (FRESH / "runtime-extra" / ".pack-version").read_text(
            encoding="utf-8").strip() == "runtime-v1")
        k2 = requests.get(B + "/api/kernel/status", headers=H, timeout=10).json()
        chk("kernel/status 感知运行时", k2.get("runtime_pack") is True, str(k2.get("runtime_pack")))
        h2 = requests.get(B + "/api/health", timeout=10).json()["camoufox"]
        chk("sys.path 注入即时生效",
            h2["parts"]["浏览器运行时"] is True and "没加载" not in h2["reason"], h2["reason"][:60])
        chk("下一步引导指向内核",
            "内核尚未安装" in h2["reason"], h2["reason"][:60])
    except Exception as e:
        chk("运行时包", False, f"{type(e).__name__}: {e}")

    print("=== 接口与主题持久化 ===", flush=True)
    try:
        chk("profiles 为空数组", requests.get(B + "/api/profiles", headers=H, timeout=10).json() == [])
        chk("无 token -> 401", requests.get(B + "/api/profiles", timeout=10).status_code == 401)
        chk("settings 默认暗色", requests.get(B + "/api/settings", headers=H, timeout=10).json()["theme"] == "dark")
        requests.put(B + "/api/settings", headers=H, json={"theme": "light", "close_action": "tray"}, timeout=10)
        got = requests.get(B + "/api/settings", headers=H, timeout=10).json()
        chk("主题写入后回读", got["theme"] == "light" and (FRESH / "settings.json").exists())
        requests.put(B + "/api/settings", headers=H, json={"theme": "dark"}, timeout=10)
    except Exception as e:
        chk("接口冒烟", False, str(e))

    print("=== 新策略：不裁剪 / 不锁系统 / 时区留空 / 只提示不自动装 ===", flush=True)
    try:
        s = requests.get(B + "/api/kernel/status", headers=H, timeout=10).json()
        chk("内核升级只提示不自动装", s.get("auto_install") is False, str(s.get("auto_install")))
        chk("字库裁剪已停用", s.get("trim_enabled") is False, str(s.get("trim_enabled")))
        chk("内核未装时放行三系统",
            set(s.get("supported_os") or []) == {"windows", "macos", "linux"},
            "/".join(s.get("supported_os") or []))
        chk("版本/升级时长字段完整",
            all(k in s for k in ("installed_at", "age_text", "age_days", "fonts")), s.get("age_text"))
    except Exception as e:
        chk("内核策略", False, f"{type(e).__name__}: {e}")
    try:
        payload = {
            "name": "_回归_临时", "note": "", "group": "", "tags": [],
            "proxy": {"scheme": "socks5", "host": "127.0.0.1", "port": "1080",
                      "username": "", "password": ""},
            "os_name": "macos", "screen_w": 1920, "screen_h": 1080,
            "locale": "ja-JP", "timezone": "Asia/Tokyo", "start_url": "https://www.google.com",
        }
        r = requests.post(B + "/api/profiles", headers=H, json=payload, timeout=10)
        created = r.json()
        chk("可新建 macOS 环境（不锁系统）",
            r.status_code == 200 and created.get("os_name") == "macos",
            f"status={r.status_code} os={created.get('os_name')}")
        chk("手填 timezone 被强制留空", created.get("timezone") == "", repr(created.get("timezone")))
        chk("语言按填写值保存", created.get("locale") == "ja-JP", repr(created.get("locale")))
        chk("环境带 network 诊断字段", isinstance(created.get("network"), dict),
            str(created.get("network"))[:40])
        bad = requests.post(B + "/api/profiles", headers=H,
                            json=dict(payload, locale="zh_CN"), timeout=10)
        chk("非法语言格式被拒绝", bad.status_code == 400, f"status={bad.status_code}")
        if bad.status_code == 200 and bad.json().get("id"):
            requests.delete(B + "/api/profiles/" + bad.json()["id"], headers=H, timeout=10)
        if created.get("id"):
            r2 = requests.put(B + "/api/profiles/" + created["id"], headers=H,
                              json=dict(payload, locale="zh-CN"), timeout=10)
            chk("可改成 zh-CN", r2.status_code == 200 and r2.json().get("locale") == "zh-CN",
                f"status={r2.status_code} locale={r2.json().get('locale')}")
            requests.delete(B + "/api/profiles/" + created["id"], headers=H, timeout=10)
            left = requests.get(B + "/api/profiles", headers=H, timeout=10).json()
            chk("临时环境已清理", all(p["id"] != created["id"] for p in left))
    except Exception as e:
        chk("环境策略", False, f"{type(e).__name__}: {e}")

    print("=== CLI（经 exe 调用） ===", flush=True)
    try:
        p = subprocess.run([str(EXE), "--cli", "status"], env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=90)
        # CLI 输出是缩进过的多行 JSON，要整体解析，不能只取最后一行
        d = json.loads(p.stdout) if p.stdout.strip() else {}
        chk("exe --cli status", p.returncode == 0 and d.get("ok") is True,
            f"exit={p.returncode} stderr={len(p.stderr)}B")
    except Exception as e:
        chk("exe --cli status", False, f"{type(e).__name__}: {e}")

    print("=== 界面截图 ===", flush=True)
    try:
        sys.path.insert(0, str(TOOLS / ".." / "_dev"))
        from shot import grab
        u.ShowWindow(hwnd, 9); u.SetForegroundWindow(hwnd)
        time.sleep(1.2)
        grab(str(TOOLS / "exe_ui.bmp"), 1, (0, 0, 1400, 900))
        chk("截图已保存", (TOOLS / "exe_ui.bmp").exists(), str(TOOLS / "exe_ui.bmp"))
    except Exception as e:
        chk("截图", False, f"{type(e).__name__}: {e}")

    print("=== 退出 ===", flush=True)
    try:
        requests.post(B + "/api/shutdown", headers=H, timeout=10)
    except Exception:
        pass
    for _ in range(40):
        if proc.poll() is not None:
            break
        time.sleep(0.4)
    chk("exe 进程已退出", proc.poll() is not None)
    chk("runtime.json 已清理", not rt_file.exists())
    chk("账号数据目录保留", (FRESH / "app.db").exists())

    print(f"\n==== {sum(results)}/{len(results)} 通过 ====", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
