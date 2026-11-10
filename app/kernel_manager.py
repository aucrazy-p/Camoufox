"""
浏览器内核的下载、升级与回滚。

设计原则：
  1. 绝不后台自动下载。所有下载都必须由用户在界面点「开始」后调 install() 触发。
     版本检查只是只读查询官方 Releases，不会触发任何下载。
  2. 只用官方源（GitHub Releases），不试任何加速镜像。
     失败就把原始错误交回界面，由用户决定重试 / 换代理 / 手动指定本地 zip。
  3. **不再裁剪字库**。官方 font-bundle 是独立迭代的构建资源
     （v1 1513 files → v4 1532 files，且会改「哪些字体服务哪个 OS 身份」），
     按文件名维护黑白名单跨版本必然失配，还可能中断升级。保留完整字库后
     macOS / Linux 身份也可用，代价只是约 1.5GB 硬盘。
  4. 替换是原子的：新内核先在临时目录完整准备好，再一次性移动；旧版留备份可回滚。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import traceback
import zipfile
from pathlib import Path

import requests

import common
import policy

RELEASES_API = "https://api.github.com/repos/daijro/camoufox/releases"
ASSET_SUFFIX = "win.x86_64.zip"

_state = {
    "phase": "idle",       # idle / downloading / verifying / extracting / installing / done / error
    "percent": 0,
    "message": "",
    "error": "",
    "version": "",
    "downloaded": 0,
    "total": 0,
}
_lock = threading.Lock()
_cancel = threading.Event()
_worker: threading.Thread | None = None


# ---------------------------------------------------------------- 状态

def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def progress() -> dict:
    with _lock:
        d = dict(_state)
    d["cancelled"] = _cancel.is_set()
    return d


def status() -> dict:
    exe = common.kernel_exe()
    size = 0
    if exe:
        try:
            size = sum(f.stat().st_size for f in exe.parent.rglob("*") if f.is_file())
        except OSError:
            size = 0
    try:
        free = shutil.disk_usage(common.DATA).free
    except OSError:
        free = 0
    fonts = (policy.font_bundle_info(exe.parent) if exe else
             {"files": 0, "size_mb": 0, "complete": False,
              "label": "内核未安装", "trimmed": False})
    age = policy.kernel_age()
    return {
        "installed": exe is not None,
        "version": common.kernel_version(),
        "path": str(exe.parent) if exe else "",
        "installed_at": age["installed_at"],
        "age_days": age["days"],
        "age_text": age["age_text"],
        "install_source": age["source"],
        "trimmed": fonts["trimmed"],
        "fonts": fonts,
        "size_mb": round(size / 1024 / 1024),
        "free_mb": round(free / 1024 / 1024),
        "fpgen": common.fpgen_ready(),
        "geoip": common.geoip_ready(),
        "supported_os": common.supported_oses(),
        # 明确声明：只有用户在界面点「安装此版本」才会 install()，启动时/查版本都不会下载
        "auto_install": policy.AUTO_INSTALL_KERNEL,
        "trim_enabled": policy.TRIM_ENABLED,
        "progress": progress(),
    }


def _session(proxy: str | None = None) -> requests.Session:
    """官方源直连优先；用户明确填了代理才走代理。trust_env=False 避免被系统代理带偏。"""
    s = requests.Session()
    s.trust_env = False
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


def releases(proxy: str | None = None) -> list[dict]:
    """列出可用的 Windows x64 内核版本。失败原样抛出，不静默降级。"""
    s = _session(proxy)
    r = s.get(RELEASES_API, timeout=(10, 30))
    r.raise_for_status()
    out = []
    for rel in r.json():
        for a in rel.get("assets", []):
            name = a.get("name", "")
            if not name.endswith(ASSET_SUFFIX):
                continue
            digest = a.get("digest") or ""
            out.append({
                "tag": rel.get("tag_name", ""),
                "name": name,
                "version": name.replace("camoufox-", "").replace("-" + ASSET_SUFFIX.replace("win.x86_64.zip", ""), ""),
                "size": a.get("size", 0),
                "url": a.get("browser_download_url", ""),
                "sha256": digest.split(":", 1)[1] if digest.startswith("sha256:") else "",
                "published": rel.get("published_at", ""),
                "prerelease": bool(rel.get("prerelease")),
            })
    return out


def _log(msg: str) -> None:
    try:
        with common.log_file("kernel.log").open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except OSError:
        pass
    _set(message=msg)


# ---------------------------------------------------------------- 安装流程

def install(version: str = "", local_zip: str = "", proxy: str = "") -> dict:
    """在后台线程启动安装。version 为空=最新；local_zip 指定本地 zip 可完全离线。"""
    global _worker
    if _worker is not None and _worker.is_alive():
        return {"ok": False, "error": "已有安装任务在进行中"}
    _cancel.clear()
    _set(phase="preparing", percent=0, message="准备中", error="", version=version or "latest",
         downloaded=0, total=0)
    _worker = threading.Thread(
        target=_install_worker, args=(version, local_zip, proxy), daemon=True,
        name="kernel-install",
    )
    _worker.start()
    return {"ok": True}


def cancel() -> dict:
    _cancel.set()
    return {"ok": True}


def rollback() -> dict:
    """回滚到上一次的内核（备份目录里名字最大的一个）。"""
    backup_root = common.KERNEL_ROOT / "_backup"
    if not backup_root.exists():
        return {"ok": False, "error": "没有可回滚的旧版本"}
    olds = sorted(
        (p for p in backup_root.iterdir() if (p / "camoufox.exe").is_file()),
        key=lambda p: p.name,
    )
    if not olds:
        return {"ok": False, "error": "备份目录里没有完整内核"}
    old = olds[-1]
    cur = common.kernel_exe()
    if cur:
        trash = common.TRASH / f"kernel_{int(time.time())}"
        trash.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(cur.parent), str(trash))
    shutil.move(str(old), str(common.KERNEL_ROOT / old.name))
    policy.write_kernel_meta(old.name, source="rollback",
                             previous=cur.parent.name if cur else "")
    _log(f"已回滚到 {old.name}")
    return {"ok": True, "version": old.name}


def _check_cancel() -> None:
    if _cancel.is_set():
        raise RuntimeError("用户已取消")


def _download(url: str, dest: Path, expected_sha: str, proxy: str) -> None:
    """流式下载 + Range 断点续传 + SHA256 校验。"""
    part = dest.with_suffix(dest.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    s = _session(proxy)
    with s.get(url, stream=True, timeout=(15, 120), headers=headers) as r:
        if r.status_code not in (200, 206):
            raise RuntimeError(f"下载失败 HTTP {r.status_code}：{r.text[:200]}")
        if r.status_code == 200:
            have = 0
            mode = "wb"
        else:
            mode = "ab"
        total = int(r.headers.get("Content-Length", 0)) + have
        _set(total=total, downloaded=have, phase="downloading")
        with part.open(mode) as f:
            for chunk in r.iter_content(1024 * 512):
                if not chunk:
                    continue
                _check_cancel()
                f.write(chunk)
                have += len(chunk)
                _set(downloaded=have, percent=min(45, int(have / total * 45)) if total else 0,
                     message=f"下载内核 {have/1024/1024:.0f} / {total/1024/1024:.0f} MB")

    if expected_sha:
        _set(phase="verifying", message="校验 SHA256")
        h = hashlib.sha256()
        with part.open("rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                _check_cancel()
                h.update(block)
        if h.hexdigest().lower() != expected_sha.lower():
            raise RuntimeError("SHA256 校验不通过，文件可能损坏或中途被篡改")
    part.replace(dest)


def _extract(zip_path: Path, out_dir: Path) -> Path:
    """解压并定位含 camoufox.exe 的那一层目录。"""
    _set(phase="extracting", percent=50, message="解压内核（约 2.5GB，需要几分钟）")
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        members = z.infolist()
        total = len(members)
        for i, m in enumerate(members):
            _check_cancel()
            z.extract(m, out_dir)
            if i % 200 == 0:
                _set(percent=50 + int(i / max(total, 1) * 20),
                     message=f"解压 {i} / {total} 个文件")
    candidates = [p.parent for p in out_dir.rglob("camoufox.exe") if p.is_file()]
    if not candidates:
        raise RuntimeError("压缩包里找不到 camoufox.exe，可能不是官方 Windows 内核包")
    # 取层级最浅的那个，避免误选到子目录里的副本
    return sorted(candidates, key=lambda p: len(p.parts))[0]


def inspect_fonts(kernel_dir: Path) -> dict:
    """只读统计字库完整度。**不再删除任何字体文件**。

    官方 font-bundle 独立迭代，按文件名裁剪跨版本必然失配；保留完整 bundle
    还能让 macOS / Linux 身份正常渲染。代价只是约 1.5GB 硬盘。
    """
    return policy.font_bundle_info(kernel_dir)


def _install_extras(proxy: str) -> str:
    """GeoIP 库是首次安装的可选组件，失败不阻断内核安装。"""
    if common.geoip_ready():
        return "GeoIP 已就位"
    _set(message="下载 GeoIP 数据库（约 50MB）")
    keys = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
    old = {k: os.environ.get(k) for k in keys}
    try:
        # download_mmdb 内部用 requests(trust_env=True)，代理只能通过环境变量传进去。
        # 默认是直连；失败不阻断内核安装，只影响「时区跟随出口 IP」。
        for k in keys:
            if proxy:
                os.environ[k] = proxy
            else:
                os.environ.pop(k, None)
        from camoufox.geolocation import download_mmdb
        download_mmdb()
        return "GeoIP 下载完成" if common.geoip_ready() else "GeoIP 下载后仍未就位"
    except Exception as exc:
        return f"GeoIP 下载失败（不影响启动，时区不自动对齐）：{type(exc).__name__}: {exc}"
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _install_worker(version: str, local_zip: str, proxy: str) -> None:
    try:
        common.prepare_env()
        proxy = (proxy or common.load_settings().get("download_proxy") or "").strip()
        zip_path: Path | None = None

        if local_zip:
            zip_path = Path(local_zip)
            if not zip_path.is_file():
                raise RuntimeError(f"本地文件不存在：{local_zip}")
            _set(version=zip_path.stem)
        else:
            _set(message="查询官方版本列表")
            items = releases(proxy)
            if not items:
                raise RuntimeError("官方 Releases 里没有找到 Windows x64 内核")
            pick = next((x for x in items if x["tag"] == version), None) if version else items[0]
            if pick is None:
                raise RuntimeError(f"找不到指定版本 {version}")
            _set(version=pick["tag"], total=pick["size"])
            _log(f"准备下载 {pick['name']} ({pick['size']/1024/1024:.0f} MB)")
            zip_path = common.DOWNLOADS / pick["name"]
            if not zip_path.exists():
                _download(pick["url"], zip_path, pick["sha256"], proxy)

        _check_cancel()
        staged = common.DOWNLOADS / "_staged"
        kernel_dir = _extract(zip_path, staged)
        # 不再裁剪字库：完整保留三套字体，macOS / Linux 身份也能正常渲染
        fonts = inspect_fonts(kernel_dir)
        _log(f"字库: {fonts['files']} 个文件 / {fonts['size_mb']} MB（{fonts['label']}）")
        _check_cancel()

        _set(phase="installing", percent=82, message="安装内核（原子替换）")
        name = kernel_dir.name
        target = common.KERNEL_ROOT / name
        common.KERNEL_ROOT.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)

        cur = common.kernel_exe()
        previous_version = cur.parent.name if cur else ""
        if cur and cur.parent != target:
            backup_root = common.KERNEL_ROOT / "_backup"
            backup_root.mkdir(parents=True, exist_ok=True)
            bak = backup_root / cur.parent.name
            if bak.exists():
                shutil.rmtree(bak, ignore_errors=True)
            shutil.move(str(cur.parent), str(bak))
            _log(f"旧内核已备份到 {bak}")

        shutil.move(str(kernel_dir), str(target))
        shutil.rmtree(staged, ignore_errors=True)

        extra = _install_extras(proxy)
        _log(extra)
        # 内核换了，显卡缓存必须作废（路径变了自然会重探，这里显式清一次更保险）
        (common.DATA / "host_gpu.json").unlink(missing_ok=True)
        policy.write_kernel_meta(name, source="local" if local_zip else "download",
                                 previous=previous_version)

        _set(phase="done", percent=100, message=f"内核 {name} 安装完成；{extra}")
        _log(f"完成：{target}")
    except Exception as exc:
        if _cancel.is_set():
            _set(phase="idle", message="已取消", error="用户已取消")
        else:
            raw = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, requests.exceptions.RequestException):
                # 国内直连 GitHub 资产常被重置/超时。给出可操作指引，绝不自动换第三方镜像。
                hint = "连不上官方源：请在「下载代理」填本机代理后重试，或用「指定本地 zip」离线安装"
                _set(phase="error",
                     error=f"{hint}。不会自动尝试第三方镜像。原始错误：{raw}",
                     message=f"下载失败：{hint}")
            else:
                _set(phase="error", error=raw, message=f"失败：{exc}")
            _log("失败：" + traceback.format_exc())
    finally:
        _cancel.clear()
