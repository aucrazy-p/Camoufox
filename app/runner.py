"""
单环境启动器 —— 由 app.py 以子进程方式拉起，一个环境一个进程。

指纹锁定分两层，这是本工具最关键的一处设计：

  fingerprint.json  设备的身份（屏幕/GPU/字体/Canvas 种子…），一经生成永不变
  config.json       首次启动时 Camoufox 实际发出的完整配置 + 当时的代理签名

  代理没变  -> 原样注入 config.json，连时区经纬度都和上次一模一样
  代理变了  -> 只注入 fingerprint.json（设备不变），时区/语言/经纬度按新 IP 重算

这样既保证「同一环境永远是同一台机器」，又不会因为换了代理导致时区还停在旧地区。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

common.prepare_env()

CHUNK_WIN = 2047
CHUNK_OTHER = 32767


def log(*args) -> None:
    print(*args, flush=True)


def _start_offscreen_mover(stop: threading.Event) -> threading.Thread | None:
    """显卡探测期间，把临时浏览器窗口挪到屏幕外。

    不隐藏、不最小化：那样 Firefox 可能暂停合成，WebGL 读回来的设备字符串会变，
    反而把指纹搞坏。只改坐标，窗口尺寸和渲染路径都不动。

    只移动「探测开始后新出现的第一个」Mozilla 窗口，移完立即退出。
    这样即使有人同时启动两个环境，也不会把另一个环境的主窗口拖走。
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    u = ctypes.windll.user32
    CALLBACK = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def mozilla_windows() -> set[int]:
        found: set[int] = set()

        def cb(hwnd, _):
            if not u.IsWindowVisible(hwnd):
                return True
            cls = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(hwnd, cls, 256)
            if cls.value != "MozillaWindowClass":
                return True
            # 不过滤尺寸：窗口刚创建时可能还很小，等它长到 1296x944 再挪
            # 就已经在屏幕上闪了将近 1 秒。探测浏览器只会创建一个主窗口。
            found.add(int(hwnd))
            return True

        ref = CALLBACK(cb)
        u.EnumWindows(ref, 0)
        return found

    baseline = mozilla_windows()
    sw, _ = common.real_screen()
    off_x = -(sw + 400)
    # SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
    flags = 0x0001 | 0x0004 | 0x0010

    def loop() -> None:
        while not stop.is_set():
            for hwnd in mozilla_windows() - baseline:
                u.SetWindowPos(hwnd, 0, off_x, 0, 0, 0, flags)
                return  # 一次探测只会有一个窗口，移完就收手
            if stop.wait(0.02):
                return

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def _install_host_gpu_cache() -> None:
    """把「主机显卡探测」的结果缓存到磁盘，避免每次都多开一个浏览器窗口。

    Camoufox 生成启动参数时要先知道这台机器真实渲染用的显卡（决定 Canvas/WebGL 怎么处理），
    它的做法是：真开一个不带 profile 的浏览器，读一次 WebGL 渲染器，再关掉。
    这个临时浏览器会以默认尺寸（约 1296x944）弹出一个 "New Tab" 空白窗 ——
    用户看到的「先出现一个小浏览器窗口、再出现大窗口」就是它。

    缓存按「内核路径 + headless + 机器标识」索引：同一台机器直接复用，
    拷到另一台电脑或换了显卡会自动重新探测，绝不沿用错误的 GPU 字符串。
    真需要探测的那一次，由 _start_offscreen_mover 把临时窗口挪出屏幕，用户看不到。
    """
    try:
        import camoufox.utils as cu
    except Exception:
        return

    cache_file = common.DATA / "host_gpu.json"
    orig = cu.host_gpu
    machine = common.machine_id()

    def cached(executable_path, headless, env, prefs):
        key = "|".join([str(executable_path), str(bool(headless)), machine])
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if data.get("key") == key:
            gpu = data.get("gpu")
            return tuple(gpu) if gpu else None

        stop = threading.Event()
        mover = _start_offscreen_mover(stop)
        try:
            gpu = orig(executable_path, headless, env, prefs)
        finally:
            stop.set()
            if mover is not None:
                mover.join(timeout=2)
        try:
            cache_file.write_text(
                json.dumps(
                    {"key": key, "gpu": list(gpu) if gpu else None},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            log(f"显卡: 探测结果已缓存 -> {gpu}")
        except OSError:
            pass
        return gpu

    cu.host_gpu = cached


def browser_alive(exe_name: str = "camoufox.exe") -> bool:
    """
    浏览器进程是否还活着。直接问操作系统，不经过 Playwright。

    为什么需要这个：浏览器被强杀（或崩溃）时，driver 那边的请求会一直挂着不返回，
    ctx.pages 既不抛异常也不返回，纯靠轮询会把启动器永久卡住。
    """
    if os.name != "nt":
        try:
            return exe_name in subprocess.run(
                ["ps", "-A", "-o", "comm"], capture_output=True, text=True, timeout=5
            ).stdout
        except Exception:
            return True
    try:
        # 注意：中文系统的 tasklist 输出是 GBK，不能按 UTF-8 解码，也不能用 text=True
        res = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/NH"],
            capture_output=True,
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        text = (res.stdout or b"").decode("mbcs", errors="ignore")
        return exe_name.lower() in text.lower()
    except Exception:
        return True  # 判断不了就当它还活着，不要误杀


def watch_browser(profile_dir: Path, grace: float = 5.0, interval: float = 3.0) -> None:
    """看门狗线程：浏览器进程全没了就收摊。"""

    def _loop() -> None:
        time.sleep(grace)
        gone = 0
        while True:
            if browser_alive():
                gone = 0
            else:
                gone += 1
                if gone >= 2:
                    log("检测到浏览器进程已全部退出，启动器随之关闭")
                    try:
                        (profile_dir / ".pid").unlink(missing_ok=True)
                    except OSError:
                        pass
                    os._exit(0)
            time.sleep(interval)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()


def load_profile(profile_id: str) -> dict:
    conn = sqlite3.connect(common.DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise SystemExit(f"环境 {profile_id} 不存在")
    return dict(row)


def build_proxy(cfg: dict) -> dict | None:
    host = (cfg.get("proxy_host") or "").strip()
    port = (cfg.get("proxy_port") or "").strip()
    if not host or not port:
        return None
    scheme = (cfg.get("proxy_scheme") or "socks5").strip().lower()
    proxy: dict[str, str] = {"server": f"{scheme}://{host}:{port}"}
    user = (cfg.get("proxy_user") or "").strip()
    if user:
        proxy["username"] = user
        proxy["password"] = cfg.get("proxy_pass") or ""
    return proxy


def proxy_signature(cfg: dict) -> str:
    """代理变了就要重算 geoip，所以只取出口相关字段做签名。"""
    host = (cfg.get("proxy_host") or "").strip()
    if not host:
        return "direct"
    return "|".join(
        [
            (cfg.get("proxy_scheme") or "socks5").strip().lower(),
            host,
            (cfg.get("proxy_port") or "").strip(),
        ]
    )


def config_signature(cfg: dict) -> str:
    """config.json 的复用签名 = 出口 + 浏览器语言。

    proxy_signature 单独保留给网络诊断（network.json 按前 3 段解析代理地址），
    不要往里塞新字段。语言改了必须让 config.json 重算，否则 launch_options
    算出的新 locale 会被旧配置整份覆盖 —— 界面上改了却完全不生效。
    只重算启动配置，fingerprint.json 设备模板不变（不是「换一台电脑」）。
    """
    return proxy_signature(cfg) + "|" + (cfg.get("locale") or "en-US").strip()


def collect_config(env: dict) -> str:
    keys = sorted(
        (k for k in env if k.startswith("CAMOU_CONFIG_")),
        key=lambda s: int(s.rsplit("_", 1)[1]),
    )
    return "".join(env[k] for k in keys if isinstance(env[k], str))


def inject_config(env: dict, config_json: str) -> None:
    for k in [k for k in list(env) if k.startswith("CAMOU_CONFIG_")]:
        env.pop(k, None)
    chunk = CHUNK_WIN if os.name == "nt" else CHUNK_OTHER
    for i in range(0, len(config_json), chunk):
        env[f"CAMOU_CONFIG_{i // chunk + 1}"] = config_json[i : i + chunk]


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("用法: python runner.py <profile_id>")
    profile_id = sys.argv[1]

    profile_dir = common.PROFILES / profile_id
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / ".pid").write_text(str(os.getpid()), encoding="utf-8")

    try:
        _run(profile_id, profile_dir)
    except SystemExit:
        raise
    except Exception:
        log("启动失败：")
        log(traceback.format_exc())
        raise
    finally:
        (profile_dir / ".pid").unlink(missing_ok=True)


def _run(profile_id: str, profile_dir: Path) -> None:
    cfg = load_profile(profile_id)
    log(f"环境: {cfg['name']}  ({profile_id})")

    kernel = common.kernel_exe()
    if kernel is None:
        raise SystemExit("浏览器内核缺失：data\\kernel\\<版本>\\camoufox.exe 不存在")

    wanted_os = (cfg.get("os_name") or "windows").strip().lower()
    if wanted_os not in common.supported_oses():
        raise SystemExit(
            f"环境声明为 {wanted_os}，但当前内核只支持 "
            f"{('/'.join(common.supported_oses()))}。\n"
            "这是一份旧版裁剪过的内核（只剩 Windows 字库）。新装内核已不裁剪，\n"
            "在「内核管理」里装一次即可使用完整三系统字库；或者把\n"
            "_trim\\fonts-not-win\\ 下的字体拷回内核 fonts 目录并删除 TRIM_WINDOWS_ONLY.txt。"
        )

    from camoufox.fingerprints import Screen, generate_fingerprint
    from camoufox.utils import launch_options
    from playwright.sync_api import sync_playwright

    _install_host_gpu_cache()

    log(f"内核: {kernel.parent.name}")

    # 虚构屏幕不能超过真实显示器，否则会露出“窗口比屏幕大”的破绽
    real_w, real_h = common.real_screen()
    want_w = int(cfg.get("screen_w") or 1920)
    want_h = int(cfg.get("screen_h") or 1080)
    scr_w, scr_h = min(want_w, real_w), min(want_h, real_h)
    if (scr_w, scr_h) != (want_w, want_h):
        log(f"屏幕 {want_w}x{want_h} 超过本机 {real_w}x{real_h}，已收敛为 {scr_w}x{scr_h}")

    proxy = build_proxy(cfg)
    sig = proxy_signature(cfg)       # 网络诊断用，格式不变
    csig = config_signature(cfg)     # config.json 复用判定用（含语言）
    if proxy:
        log(f"代理: {proxy['server']}  (认证: {'是' if proxy.get('username') else '否'})")
    else:
        log("代理: 未配置 —— 使用本机网络出口，多账号场景下会导致关联")

    geoip = common.geoip_ready()
    if not geoip:
        log("GeoIP 库缺失：时区不会跟随出口 IP，将使用 fingerprint.json 里的固定值")

    fp_file = profile_dir / "fingerprint.json"
    cfg_file = profile_dir / "config.json"

    saved_fp: dict | None = None
    if fp_file.exists():
        try:
            saved_fp = json.loads(fp_file.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            saved_fp = None
            log("fingerprint.json 损坏，将重新生成")

    # 设备身份先定下来，再拿它去生成启动参数。
    # 顺序很关键：必须让 config.json 和 fingerprint.json 描述的是同一台机器，
    # 否则「换代理」时回落到 fingerprint.json，设备就变了。
    if not saved_fp:
        saved_fp = generate_fingerprint(
            screen=Screen(min_width=scr_w, max_width=scr_w, min_height=scr_h, max_height=scr_h),
            os=cfg.get("os_name") or "windows",
        )
        fp_file.write_text(json.dumps(saved_fp, ensure_ascii=False, indent=1), encoding="utf-8")
        log("指纹: 首次生成并已锁定（fingerprint.json）")

    kwargs: dict = {
        "headless": False,
        "os": cfg.get("os_name") or "windows",
        "locale": cfg.get("locale") or "en-US",
        "humanize": True,
        "enable_cache": True,
        "executable_path": kernel,
        "geoip": geoip,
        "fingerprint": saved_fp,
        "i_know_what_im_doing": True,  # 指纹由我们自己锁定，不是随机生成
    }
    if proxy:
        kwargs["proxy"] = proxy
    if cfg.get("timezone"):
        kwargs["timezone"] = cfg["timezone"]

    try:
        opts = launch_options(**kwargs)
    except Exception as exc:
        log(f"带 geoip 的启动参数生成失败（{exc}），改为不自动对齐时区")
        kwargs["geoip"] = False
        opts = launch_options(**kwargs)

    env = dict(opts.get("env") or {})

    # 每次启动都刷新「出口诊断」。即使下面复用锁定的 config.json，这里用的也是
    # 本次 launch_options(geoip=True) 新算出的值。geoip 探测本来就会发生，
    # 不额外发请求、不往网页注入任何脚本；管理页直接读这份 network.json。
    import policy

    fresh_config = collect_config(env)
    proxy_server = proxy["server"] if proxy else ""
    net = policy.write_network(profile_dir, fresh_config, sig, proxy_server)
    if net.get("exit_ip"):
        log(f"出口: {net['exit_ip']}  时区: {net['timezone'] or '—'}  "
            f"地区: {net['region'] or '—'}  代理: {'已生效' if net['proxied'] else '未配置（直连）'}")

    # ---- 第一层：代理没变就原样复用上次的完整配置 ----
    reuse_config = False
    if cfg_file.exists():
        try:
            saved = json.loads(cfg_file.read_text(encoding="utf-8"))
            # 旧版 config.json 没有 config_sig，会在下次启动时自然重算一次
            if saved.get("config_sig") == csig and saved.get("config"):
                inject_config(env, saved["config"])
                reuse_config = True
                log("指纹: 复用锁定的完整配置（设备、出口与语言均与上次一致）")
        except (ValueError, OSError):
            log("config.json 损坏，忽略")

    # ---- 代理变了或首次启动：记录本次完整配置 ----
    if not reuse_config:
        config_json = collect_config(env)
        if config_json:
            cfg_file.write_text(
                json.dumps({"proxy_sig": sig, "config_sig": csig,
                            "config": config_json}, ensure_ascii=False),
                encoding="utf-8",
            )
            log("指纹: 已记录本次完整配置（config.json）")
        else:
            log("警告: 未能从启动参数里读到指纹配置，本次未记录")

    opts["env"] = env

    # Playwright 默认给一个 1280x720 的固定视口，会把网页可视区钉成一小块，
    # 窗口是全屏的，右边和下边就各留一块空白。Camoufox 自己启动时会关掉视口，
    # 我们是用 Playwright 直连，必须自己关掉，让网页跟随窗口尺寸。
    opts["no_viewport"] = True

    opts = {k: v for k, v in opts.items() if v is not None}

    user_data = profile_dir / "browser"
    user_data.mkdir(parents=True, exist_ok=True)
    # 不要在这里强行最大化 / 改 xulstore / 清 sessionstore。
    # Camoufox 会按 fingerprint.json 里的 screen.availWidth/availHeight 设定窗口尺寸，
    # 让真实窗口和 window.screen 一致；外部把它拉成别的尺寸反而会造成指纹矛盾。

    start_url = (cfg.get("start_url") or "https://www.google.com").strip()

    with sync_playwright() as pw:
        ctx = pw.firefox.launch_persistent_context(str(user_data), **opts)
        log("浏览器已启动")
        watch_browser(profile_dir)

        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(start_url, wait_until="domcontentloaded", timeout=90_000)
        except Exception as exc:
            log(f"打开首页失败（可手动输入网址）：{exc}")

        # 等窗口关闭。这里容易踩坑：关掉最后一个窗口后 ctx.pages 只是变成空列表，
        # 不会抛异常，所以不能靠 try/except 判断退出，要同时看事件和空列表。
        state = {"closed": False}

        def _on_close(_payload=None):
            state["closed"] = True

        try:
            ctx.on("close", _on_close)
        except Exception:
            pass

        empty_rounds = 0
        while not state["closed"]:
            time.sleep(1.0)
            try:
                pages = ctx.pages
            except Exception:
                break  # 浏览器进程没了，driver 会在这里抛
            if not pages:
                empty_rounds += 1
                if empty_rounds >= 3:
                    break
            else:
                empty_rounds = 0

        try:
            ctx.close()
        except Exception:
            pass

    log("浏览器已关闭，进程退出")


if __name__ == "__main__":
    main()
