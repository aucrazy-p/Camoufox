"""
浏览器运行时包的按需安装。

拆出去的 Playwright Node driver / numpy / fpgen / camoufox 不进 exe，
首次使用由用户在界面手动装到 CamoufoxData\\runtime-extra，之后常驻，换 exe 不重装。

原则与 kernel_manager.py 完全一致：
  1. 绝不后台自动下载。只有用户在界面点「下载并安装」才会触发 install()。
  2. 只用本仓库 Releases 的**固定 tag**（exe 内置，不追 latest），
     不试任何第三方加速镜像。
  3. 失败只把原始错误交回界面，由用户决定重试 / 填代理 / 指定本地 zip。
  4. 解压到暂存目录完整校验后一次性原子替换，写 .pack-version 标记。
"""
from __future__ import annotations

import hashlib
import shutil
import threading
import time
import traceback
import zipfile
from pathlib import Path

import requests

import common

REPO = "aucrazy-p/Camoufox"
TAG = common.RUNTIME_TAG
ZIP_NAME = "runtime-extra-v1.zip"
SHA_NAME = ZIP_NAME + ".sha256"
RELEASE_BASE = f"https://github.com/{REPO}/releases/download/{TAG}"
URL_ZIP = f"{RELEASE_BASE}/{ZIP_NAME}"
URL_SHA = f"{RELEASE_BASE}/{SHA_NAME}"
SIZE_HINT_MB = 70

# 装完必须存在的条目（文件，不是目录）
KEY_FILES = (
    "playwright/driver/node.exe",
    "camoufox/utils.py",
    "numpy/__init__.py",
    "fpgen/__init__.py",
    ".pack-version",
)

_state = {
    "phase": "idle",   # idle / downloading / verifying / extracting / installing / done / error
    "percent": 0,
    "message": "",
    "error": "",
    "downloaded": 0,
    "total": 0,
}
_lock = threading.Lock()
_cancel = threading.Event()
_worker: threading.Thread | None = None


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def progress() -> dict:
    with _lock:
        d = dict(_state)
    d["cancelled"] = _cancel.is_set()
    return d


def status() -> dict:
    installed = common.runtime_pack_ready()
    size = 0
    if common.RUNTIME_EXTRA.is_dir():
        try:
            size = sum(f.stat().st_size for f in common.RUNTIME_EXTRA.rglob("*") if f.is_file())
        except OSError:
            size = 0
    return {
        "installed": installed,
        "tag": TAG,
        "version": TAG,
        "path": str(common.RUNTIME_EXTRA),
        "size_mb": round(size / 1024 / 1024, 1),
        "zip_mb_hint": SIZE_HINT_MB,
        "url": URL_ZIP,
        "modules": {
            k: (common.RUNTIME_EXTRA / k).is_file() for k in KEY_FILES
        },
        # 明确声明：查状态绝不会触发下载
        "auto_install": False,
        "progress": progress(),
    }


def _session(proxy: str | None = None) -> requests.Session:
    """直连优先；用户明确填了代理才走代理。不读系统代理，避免被带偏。"""
    s = requests.Session()
    s.trust_env = False
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


def _log(msg: str) -> None:
    try:
        with common.log_file("runtime.log").open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except OSError:
        pass
    _set(message=msg)


def install(local_zip: str = "", proxy: str = "") -> dict:
    """在后台线程启动安装。local_zip 指定本地 zip 可完全离线。"""
    global _worker
    if _worker is not None and _worker.is_alive():
        return {"ok": False, "error": "已有安装任务在进行中"}
    _cancel.clear()
    _set(phase="preparing", percent=0, message="准备中", error="",
         downloaded=0, total=0)
    _worker = threading.Thread(
        target=_install_worker, args=(local_zip, proxy), daemon=True,
        name="runtime-install",
    )
    _worker.start()
    return {"ok": True}


def cancel() -> dict:
    _cancel.set()
    return {"ok": True}


def _check_cancel() -> None:
    if _cancel.is_set():
        raise RuntimeError("用户已取消")


def _fetch_sha(proxy: str) -> str:
    """取 Release 上的 .sha256。取不到只记日志跳过校验，不阻断安装。"""
    try:
        r = _session(proxy).get(URL_SHA, timeout=(10, 30))
        r.raise_for_status()
        return r.text.strip().split()[0]
    except Exception as exc:
        _log(f"校验值获取失败（跳过校验）：{type(exc).__name__}: {exc}")
        return ""


def _download(dest: Path, proxy: str) -> None:
    """流式下载 + Range 断点续传。GitHub 资产会 302，requests 自动跟随。"""
    part = dest.with_suffix(dest.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    with _session(proxy).get(URL_ZIP, stream=True, timeout=(15, 120), headers=headers) as r:
        if r.status_code not in (200, 206):
            raise RuntimeError(f"下载失败 HTTP {r.status_code}：{r.text[:200]}")
        mode = "wb" if r.status_code == 200 else "ab"
        if mode == "wb":
            have = 0
        total = int(r.headers.get("Content-Length", 0)) + have
        _set(total=total, downloaded=have, phase="downloading")
        with part.open(mode) as f:
            for chunk in r.iter_content(1024 * 512):
                if not chunk:
                    continue
                _check_cancel()
                f.write(chunk)
                have += len(chunk)
                _set(downloaded=have,
                     percent=min(60, int(have / total * 60)) if total else 0,
                     message=f"下载运行时 {have/1024/1024:.0f} / {total/1024/1024:.0f} MB")
    part.replace(dest)


def _verify_sha(path: Path, expected: str) -> None:
    if not expected:
        _log("没有可用的校验值，跳过 SHA256 校验")
        return
    _set(phase="verifying", percent=65, message="校验 SHA256")
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            _check_cancel()
            h.update(block)
    if h.hexdigest().lower() != expected.lower():
        raise RuntimeError("SHA256 校验不通过，文件可能损坏或中途被篡改")


def _install_worker(local_zip: str, proxy: str) -> None:
    try:
        common.prepare_env()
        proxy = (proxy or common.load_settings().get("download_proxy") or "").strip()

        if local_zip:
            zip_path = Path(local_zip)
            if not zip_path.is_file():
                raise RuntimeError(f"本地文件不存在：{local_zip}")
            _log(f"使用本地包 {zip_path}")
            # 同名 .sha256 在旁边就顺手校验，没有就跳过
            sidecar = zip_path.with_suffix(zip_path.suffix + ".sha256")
            expected = ""
            if sidecar.is_file():
                try:
                    expected = sidecar.read_text(encoding="ascii").strip().split()[0]
                except (OSError, ValueError, IndexError):
                    expected = ""
        else:
            _log(f"准备下载 {URL_ZIP}")
            expected = _fetch_sha(proxy)
            zip_path = common.DOWNLOADS / ZIP_NAME
            _download(zip_path, proxy)

        _verify_sha(zip_path, expected)
        _check_cancel()

        _set(phase="extracting", percent=70, message="解压浏览器运行时")
        staged = common.DATA / "runtime-extra.staged"
        shutil.rmtree(staged, ignore_errors=True)
        staged.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as z:
            for m in z.infolist():
                parts = Path(m.filename).parts
                if m.filename.startswith(("/", "\\")) or ".." in parts:
                    raise RuntimeError(f"压缩包里有不安全路径：{m.filename}")
            z.extractall(staged)

        for rel in KEY_FILES:
            if not (staged / rel).is_file():
                raise RuntimeError(f"压缩包里缺少 {rel}，可能不是本项目的运行时包")
        marker = (staged / ".pack-version").read_text(encoding="utf-8").strip()
        if marker != TAG:
            raise RuntimeError(f"运行时包版本不匹配：包内 {marker}，本程序需要 {TAG}")

        _set(phase="installing", percent=88, message="安装（原子替换）")
        target = common.RUNTIME_EXTRA
        backup = common.DATA / "runtime-extra.old"
        shutil.rmtree(backup, ignore_errors=True)
        if target.exists():
            shutil.move(str(target), str(backup))
        shutil.move(str(staged), str(target))
        shutil.rmtree(backup, ignore_errors=True)
        common.refresh_runtime_path()

        _set(phase="done", percent=100, message=f"浏览器运行时 {TAG} 安装完成，可直接启动环境")
        _log(f"完成：{target}")
    except Exception as exc:
        if _cancel.is_set():
            _set(phase="idle", message="已取消", error="用户已取消")
        else:
            raw = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, requests.exceptions.RequestException):
                hint = "连不上 GitHub Releases：请在「下载代理」填本机代理后重试，或用「指定本地 zip」离线安装"
                _set(phase="error",
                     error=f"{hint}。不会自动尝试第三方镜像。原始错误：{raw}",
                     message=f"下载失败：{hint}")
            else:
                _set(phase="error", error=raw, message=f"失败：{exc}")
            _log("失败：" + traceback.format_exc())
    finally:
        _cancel.clear()
