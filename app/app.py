"""
指纹环境管理器 —— 本地多环境隔离控制台（HTTP API 层）

只监听 127.0.0.1。所有账号数据留在数据目录，程序文件与数据分离。
API 契约保持稳定，供后续自动化调用；/api/v1/* 是 /api/* 的版本化别名。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
import kernel_manager  # noqa: E402
import policy  # noqa: E402

common.prepare_env()

BASE = common.BASE
DATA = common.DATA
PROFILES = common.PROFILES
TRASH = common.TRASH
DB_PATH = common.DB_PATH
HOST = "127.0.0.1"
PORT = int(os.environ.get("FP_PORT", "8760"))
TOKEN = os.environ.get("FP_TOKEN") or secrets.token_urlsafe(24)

app = FastAPI(title="指纹环境管理器", docs_url=None, redoc_url=None)
_running: dict[str, subprocess.Popen] = {}
_shutdown_cb = None


def set_shutdown_callback(fn) -> None:
    global _shutdown_cb
    _shutdown_cb = fn


# ---------------------------------------------------------------- 安全网关

@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.scope.get("path", "")
    if path.startswith("/api/v1/"):
        path = "/api/" + path[len("/api/v1/"):]
        request.scope["path"] = path
    if path.startswith("/api/") and path != "/api/health":
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in ("127.0.0.1", "localhost", "::1"):
            return JSONResponse({"detail": "禁止的 Host"}, status_code=403)
        if request.headers.get("x-fp-token") != TOKEN:
            return JSONResponse({"detail": "未授权：缺少或错误的令牌"}, status_code=401)
    resp = await call_next(request)
    if not path.startswith("/api/"):
        # 页面与静态资源一律不缓存：首页含本次进程的令牌，端口和 URL 每次都一样，
        # 旧缓存会带着上一次的令牌；控制端升级后也不能复用旧版 JS/CSS。
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


# ---------------------------------------------------------------- 数据库

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    with _db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                id           TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                note         TEXT DEFAULT '',
                group_name   TEXT DEFAULT '',
                tags         TEXT DEFAULT '',
                proxy_scheme TEXT DEFAULT 'socks5',
                proxy_host   TEXT DEFAULT '',
                proxy_port   TEXT DEFAULT '',
                proxy_user   TEXT DEFAULT '',
                proxy_pass   TEXT DEFAULT '',
                os_name      TEXT DEFAULT 'windows',
                screen_w     INTEGER DEFAULT 1920,
                screen_h     INTEGER DEFAULT 1080,
                locale       TEXT DEFAULT 'en-US',
                timezone     TEXT DEFAULT '',
                start_url    TEXT DEFAULT 'https://www.google.com',
                created_at   INTEGER
            )
            """
        )
        cols = {r[1] for r in conn.execute("PRAGMA table_info(profiles)")}
        if "group_name" not in cols:
            conn.execute("ALTER TABLE profiles ADD COLUMN group_name TEXT DEFAULT ''")
        if "tags" not in cols:
            conn.execute("ALTER TABLE profiles ADD COLUMN tags TEXT DEFAULT ''")
        # 时区固定留空：手填值会被 Camoufox 用 config.setdefault 优先采用，
        # 覆盖 GeoIP 结果，造成「美国 IP + 手填时区」的矛盾。历史数据一律清掉（幂等）。
        conn.execute("UPDATE profiles SET timezone='' WHERE timezone IS NOT NULL AND timezone<>''")


_init_db()


# ---------------------------------------------------------------- 进程状态

def _pid_file(profile_id: str) -> Path:
    return PROFILES / profile_id / ".pid"


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def _is_running(profile_id: str) -> bool:
    proc = _running.get(profile_id)
    if proc is not None and proc.poll() is None:
        return True
    f = _pid_file(profile_id)
    if not f.exists():
        return False
    try:
        pid = int(f.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return False
    if _pid_alive(pid):
        return True
    f.unlink(missing_ok=True)
    return False


# ---------------------------------------------------------------- 依赖状态

def _camoufox_status() -> dict:
    try:
        import camoufox  # noqa: F401
    except Exception as exc:
        return {"ready": False, "reason": f"依赖未安装（{exc}）", "parts": {}}

    kernel = common.kernel_exe()
    parts = {
        "依赖": True,
        "浏览器内核": kernel is not None,
        "指纹模型": common.fpgen_ready(),
        "GeoIP 库": common.geoip_ready(),
    }
    if kernel is None:
        return {
            "ready": False,
            "reason": "浏览器内核尚未安装，请到「内核管理」页下载（约 1.3GB，保留完整字库）",
            "parts": parts,
        }
    missing = [k for k, v in parts.items() if not v]
    return {
        "ready": True,
        "reason": "" if not missing else "缺：" + "、".join(missing) + "（不影响启动，时区不自动跟随 IP）",
        "parts": parts,
        "kernel": kernel.parent.name,
        "geoip": parts["GeoIP 库"],
    }


# ---------------------------------------------------------------- 模型

class ProxyIn(BaseModel):
    scheme: str = "socks5"
    host: str = ""
    port: str = ""
    username: str = ""
    password: str = ""


class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    note: str = ""
    group: str = ""
    tags: list[str] = []
    proxy: ProxyIn = Field(default_factory=ProxyIn)
    os_name: str = "windows"
    screen_w: int = 1920
    screen_h: int = 1080
    locale: str = "en-US"
    timezone: str = ""
    start_url: str = "https://www.google.com"


def _fingerprint_summary(profile_id: str) -> dict:
    """界面显示真实指纹值，而不是配置值（两者可能不同，之前误导过）。"""
    f = PROFILES / profile_id / "fingerprint.json"
    out = {"screen": "", "locale": "", "timezone": ""}
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        s = d.get("screen") or {}
        if s.get("width"):
            out["screen"] = f"{s.get('width')}×{s.get('height')}"
        out["locale"] = (d.get("navigator") or {}).get("language", "") or ""
    except (OSError, ValueError):
        pass
    return out


def _row_to_api(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["group"] = d.pop("group_name", "") or ""
    raw_tags = d.pop("tags", "") or ""
    try:
        d["tags"] = json.loads(raw_tags) if raw_tags.startswith("[") else [t for t in raw_tags.split(",") if t]
    except (ValueError, AttributeError):
        d["tags"] = []
    d["proxy"] = {
        "scheme": d.pop("proxy_scheme") or "socks5",
        "host": d.pop("proxy_host") or "",
        "port": d.pop("proxy_port") or "",
        "username": d.pop("proxy_user") or "",
        "password": d.pop("proxy_pass") or "",
    }
    d["running"] = _is_running(d["id"])
    prof_dir = PROFILES / d["id"]
    d["has_data"] = (prof_dir / "browser").exists()
    d["fingerprint"] = _fingerprint_summary(d["id"])
    d["network"] = policy.read_network(prof_dir)
    return d


# ---------------------------------------------------------------- 基础接口

@app.get("/api/health")
def health():
    return {
        "ok": True,
        "app": common.APP_NAME,
        "version": common.VERSION,
        "camoufox": _camoufox_status(),
        "os_supported": common.supported_oses(),
        "windows_only": common.kernel_windows_only(),
    }


def _check_os(os_name: str) -> str:
    """按内核字库的实际能力放行，不做产品层锁定（用户可能新建别的系统的环境）。"""
    name = (os_name or "windows").strip().lower()
    allowed = common.supported_oses()
    if name not in allowed:
        raise HTTPException(
            400,
            f"当前内核只支持 {('/'.join(allowed))}：这是一份旧版裁剪过的内核，"
            "只剩 Windows 字库。装一次新内核（已不裁剪）即可使用 macOS / Linux 身份。",
        )
    return name


@app.get("/api/profiles")
def list_profiles():
    with _db() as conn:
        rows = conn.execute("SELECT * FROM profiles ORDER BY created_at").fetchall()
    return [_row_to_api(r) for r in rows]


_LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,2}$")


def _normalize_locale(value: str) -> str:
    """自定义语言不能乱填：非法值会让内核启动阶段直接抛 InvalidLocale。"""
    v = (value or "").strip() or "en-US"
    if not _LOCALE_RE.fullmatch(v):
        raise HTTPException(400, "语言格式不对，请写成 en-US / ja-JP / zh-CN 这样的写法")
    return v


def _profile_values(payload: ProfileIn) -> tuple:
    p = payload.proxy
    return (
        payload.name.strip(), payload.note.strip(),
        payload.group.strip(), json.dumps(payload.tags or [], ensure_ascii=False),
        p.scheme, p.host, p.port, p.username, p.password,
        _check_os(payload.os_name), payload.screen_w, payload.screen_h,
        # timezone 固定留空，交给 GeoIP 跟随出口 IP（忽略客户端传值）
        _normalize_locale(payload.locale), "", payload.start_url,
    )


@app.post("/api/profiles")
def create_profile(payload: ProfileIn):
    pid = uuid.uuid4().hex[:12]
    (PROFILES / pid).mkdir(parents=True, exist_ok=True)
    with _db() as conn:
        conn.execute(
            """INSERT INTO profiles (id,name,note,group_name,tags,proxy_scheme,proxy_host,
               proxy_port,proxy_user,proxy_pass,os_name,screen_w,screen_h,locale,timezone,
               start_url,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, *_profile_values(payload), int(time.time())),
        )
        row = conn.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
    return _row_to_api(row)


@app.put("/api/profiles/{profile_id}")
def update_profile(profile_id: str, payload: ProfileIn):
    with _db() as conn:
        cur = conn.execute(
            """UPDATE profiles SET name=?,note=?,group_name=?,tags=?,proxy_scheme=?,
               proxy_host=?,proxy_port=?,proxy_user=?,proxy_pass=?,os_name=?,screen_w=?,
               screen_h=?,locale=?,timezone=?,start_url=? WHERE id=?""",
            (*_profile_values(payload), profile_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "环境不存在")
        row = conn.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
    return _row_to_api(row)


@app.delete("/api/profiles/{profile_id}")
def delete_profile(profile_id: str):
    if _is_running(profile_id):
        raise HTTPException(400, "环境正在运行，请先停止")
    with _db() as conn:
        cur = conn.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "环境不存在")
    src = PROFILES / profile_id
    if src.exists():
        dst = TRASH / f"{profile_id}_{int(time.time())}"
        try:
            shutil.move(str(src), str(dst))
        except OSError:
            pass
    return {"ok": True}


@app.post("/api/profiles/{profile_id}/start")
def start_profile(profile_id: str):
    with _db() as conn:
        row = conn.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "环境不存在")
    if _is_running(profile_id):
        return {"ok": True, "already": True}

    status = _camoufox_status()
    if not status["ready"]:
        raise HTTPException(400, status["reason"])

    prof_dir = PROFILES / profile_id
    prof_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(prof_dir / "launcher.log", "a", encoding="utf-8", errors="replace")
    log_file.write(f"\n===== 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    log_file.flush()

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = subprocess.Popen(
        [sys.executable, str(BASE / "runner.py"), profile_id],
        cwd=str(BASE), stdout=log_file, stderr=subprocess.STDOUT,
        creationflags=flags,
    )
    _running[profile_id] = proc
    time.sleep(1.2)
    if proc.poll() is not None:
        raise HTTPException(500, f"启动失败：\n{_read_log(profile_id, 40)}")
    return {"ok": True}


@app.post("/api/profiles/{profile_id}/stop")
def stop_profile(profile_id: str):
    """先礼貌请求关闭（让 Firefox 保存会话），超时再强杀。"""
    if not _is_running(profile_id):
        return {"ok": True, "already": True}

    proc = _running.pop(profile_id, None)
    f = _pid_file(profile_id)
    pid = proc.pid if (proc is not None and proc.poll() is None) else None
    if pid is None and f.exists():
        try:
            pid = int(f.read_text(encoding="utf-8").strip())
        except ValueError:
            pid = None

    if pid and os.name == "nt":
        no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True,
                       creationflags=no_win, timeout=8)
        for _ in range(12):
            time.sleep(0.25)
            if not _pid_alive(pid):
                break
        else:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, creationflags=no_win, timeout=8)
    elif pid:
        try:
            os.kill(pid, 15)
        except OSError:
            pass

    f.unlink(missing_ok=True)
    return {"ok": True}


def _read_log(profile_id: str, lines: int = 60) -> str:
    path = PROFILES / profile_id / "launcher.log"
    if not path.exists():
        return ""
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


@app.get("/api/profiles/{profile_id}/log")
def profile_log(profile_id: str, lines: int = 80):
    return {"log": _read_log(profile_id, max(10, min(lines, 500)))}


@app.get("/api/screen")
def real_screen():
    w, h = common.real_screen()
    return {"width": w, "height": h}


# ---------------------------------------------------------------- 设置 / 系统

@app.get("/api/settings")
def get_settings():
    return common.load_settings()


@app.put("/api/settings")
def put_settings(payload: dict):
    return common.save_settings(payload)


@app.get("/api/logs")
def get_logs(name: str = "kernel.log", lines: int = 200):
    path = common.LOGS / Path(name).name
    # path 一起返回，界面上的「复制路径」按钮用；日志文本本身也能直接选中复制
    base = {"path": str(path)}
    if not path.exists():
        return {**base, "log": ""}
    try:
        text = "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
        return {**base, "log": text}
    except OSError:
        return {**base, "log": ""}


@app.get("/api/automation")
def automation_info():
    """给以后的脚本/RPA 用：服务发现信息与可直接复制的示例命令。"""
    rt = common.read_runtime() or {}
    port = rt.get("port", PORT)
    token = rt.get("token", TOKEN)
    return {
        "host": HOST,
        "port": port,
        "token": token,
        "base_url": f"http://{HOST}:{port}",
        "endpoints": [
            "GET  /api/health",
            "GET  /api/profiles",
            "POST /api/profiles/{id}/start",
            "POST /api/profiles/{id}/stop",
            "GET  /api/kernel/status",
        ],
        "curl": f'curl -H "X-FP-Token: {token}" http://{HOST}:{port}/api/profiles',
        "cli": "指纹环境管理器.exe --cli list",
        "runtime_file": str(common.RUNTIME_JSON),
    }


@app.post("/api/shutdown")
def shutdown():
    def _later():
        time.sleep(0.4)
        if _shutdown_cb:
            _shutdown_cb()
        else:
            os._exit(0)

    threading.Thread(target=_later, daemon=True).start()
    return {"ok": True}


# ---------------------------------------------------------------- 内核管理

@app.get("/api/kernel/status")
def kernel_status():
    return kernel_manager.status()


@app.get("/api/kernel/releases")
def kernel_releases(proxy: str = ""):
    try:
        return {"ok": True, "items": kernel_manager.releases(proxy or None)}
    except Exception as exc:
        raise HTTPException(502, f"查询官方版本失败：{type(exc).__name__}: {exc}")


@app.get("/api/kernel/progress")
def kernel_progress():
    return kernel_manager.progress()


@app.post("/api/kernel/install")
def kernel_install(payload: dict):
    r = kernel_manager.install(
        version=str(payload.get("version") or ""),
        local_zip=str(payload.get("local_zip") or ""),
        proxy=str(payload.get("proxy") or ""),
    )
    if not r.get("ok"):
        raise HTTPException(409, r["error"])
    return r


@app.post("/api/kernel/cancel")
def kernel_cancel():
    return kernel_manager.cancel()


@app.post("/api/kernel/rollback")
def kernel_rollback():
    r = kernel_manager.rollback()
    if not r.get("ok"):
        raise HTTPException(400, r["error"])
    return r


@app.get("/api/kernel/fonts")
def kernel_fonts():
    """只读统计字库完整度。已取消裁剪，这个接口绝不会删文件。"""
    exe = common.kernel_exe()
    if not exe:
        raise HTTPException(400, "内核尚未安装")
    return kernel_manager.inspect_fonts(exe.parent)


@app.get("/api/kernel/trim-check")
def kernel_trim_check_legacy():
    """兼容旧自动化路径，行为已改为只读统计。"""
    return kernel_fonts()


# ---------------------------------------------------------------- 页面

@app.get("/", response_class=HTMLResponse)
def index():
    """把令牌注入页面。页面本身只由本地服务提供，配合 Host 校验防跨站调用。

    占位符必须和 JS 变量名 window.__FP_TOKEN__ 完全不同：早期用同名占位符时，
    str.replace 会把变量名一起替换掉，页面拿到 undefined，所有 /api 调用 401。
    """
    f = BASE / "static" / "index.html"
    html = f.read_text(encoding="utf-8")
    if "@@FP_TOKEN@@" not in html:
        raise HTTPException(500, "首页模板缺少令牌占位符，请检查 static/index.html")
    return HTMLResponse(html.replace("@@FP_TOKEN@@", TOKEN))


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


if __name__ == "__main__":
    import uvicorn

    print(f"  指纹环境管理器  ->  http://{HOST}:{PORT}\n", flush=True)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
