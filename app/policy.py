"""
产品策略与网络诊断读取（不碰浏览器、不联网）。

集中三类小逻辑，避免散落在 app.py / runner.py / kernel_manager.py：

  1. 内核安装/升级时间 —— 用于界面显示「当前版本 / 距上次升级多久」。
  2. 字库完整度 —— 只读统计。**取消裁剪后不再删任何字体文件**，
     官方 font-bundle 会独立迭代（v1=1513 files，v4=1532 files），
     按文件名维护黑白名单跨版本必然失配，索性不维护。
  3. 每个环境最近一次启动的出口 IP / 时区 —— 直接解析 Camoufox
     `launch_options(geoip=True)` 实际下发的配置。这是浏览器真正声明的值，
     比另发一个探测请求更可信，而且零新增流量、零页面注入。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import common

KERNEL_META = common.DATA / "kernel_meta.json"

# 不再裁剪：产品策略常量，供界面与自动化接口声明用
TRIM_ENABLED = False
AUTO_INSTALL_KERNEL = False


# ---------------------------------------------------------------- 内核时间

def read_kernel_meta() -> dict:
    try:
        return json.loads(KERNEL_META.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_kernel_meta(version: str, source: str = "download", previous: str = "") -> dict:
    """安装 / 回滚成功后调用，记录本次升级时间。"""
    old = read_kernel_meta()
    meta = {
        "version": version,
        "installed_at": int(time.time()),
        "source": source,                       # download / local / rollback
        "previous": previous or old.get("version", ""),
    }
    try:
        common.DATA.mkdir(parents=True, exist_ok=True)
        tmp = KERNEL_META.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(KERNEL_META)
    except OSError:
        pass
    return meta


def kernel_age() -> dict:
    """没有 meta（老版本直接升级上来）时用内核目录 mtime 兜底。"""
    meta = read_kernel_meta()
    ts = meta.get("installed_at")
    if not ts:
        exe = common.kernel_exe()
        if exe:
            try:
                ts = int(exe.stat().st_mtime)
            except OSError:
                ts = 0
    if not ts:
        return {"installed_at": "", "days": None, "age_text": "未知",
                "source": "unknown", "version": common.kernel_version()}
    days = max(0, int((time.time() - ts) // 86400))
    text = "今天安装" if days == 0 else (f"已使用 {days} 天")
    return {
        "installed_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)),
        "days": days,
        "age_text": text,
        "source": meta.get("source") or "legacy",
        "version": meta.get("version") or common.kernel_version(),
    }


# ---------------------------------------------------------------- 字库

def font_bundle_info(kernel_dir: Path) -> dict:
    """只读统计字库。旧版裁剪过的内核约 663 个文件，完整 bundle >= 1500。"""
    empty = {"files": 0, "size_mb": 0, "complete": False,
             "label": "未知（内核未安装）", "trimmed": False}
    fonts = kernel_dir / "fonts"
    if not fonts.is_dir():
        return empty
    try:
        files = [p for p in fonts.iterdir() if p.is_file()]
        size = sum(p.stat().st_size for p in files)
    except OSError as exc:
        return {**empty, "label": f"读取失败：{exc}"}
    complete = len(files) >= 800
    return {
        "files": len(files),
        "size_mb": round(size / 1024 / 1024),
        "complete": complete,
        "trimmed": not complete,
        "label": "完整三系统字库" if complete else "旧版裁剪字库（仅 Windows）",
    }


# ---------------------------------------------------------------- 出口诊断

_NET_KEYS = (
    "webrtc:ipv4", "timezone", "locale:region", "locale:language",
    "geolocation:latitude", "geolocation:longitude", "network:ipv4Only",
)


def parse_network(config_json: str) -> dict:
    """从 CAMOU_CONFIG_* 拼回来的配置里挑出出口相关字段。"""
    try:
        c = json.loads(config_json or "{}")
    except (ValueError, TypeError):
        return {}
    return {k: c[k] for k in _NET_KEYS if k in c}


def read_network(profile_dir: Path) -> dict:
    """优先读本次启动写的 network.json；没有则从 config.json 回填上次启动的锁定配置。

    早期版本不写 network.json，但 config.json 里存着 Camoufox 实际下发的完整配置，
    出口 IP / 时区都在里面。回填后打开管理页就能看到代理效果，不必先启动一次环境。
    纯读取，不改任何文件；runner 下次启动会覆盖成新鲜探测值。
    """
    try:
        return json.loads((profile_dir / "network.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    cfg = profile_dir / "config.json"
    try:
        saved = json.loads(cfg.read_text(encoding="utf-8"))
        net = parse_network(saved.get("config") or "")
        if not net:
            return {}
        sig = saved.get("proxy_sig") or "direct"
        parts = sig.split("|") if sig != "direct" else []
        server = f"{parts[0]}://{parts[1]}:{parts[2]}" if len(parts) >= 3 else ""
        return {
            "detected_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(cfg.stat().st_mtime)),
            "proxy_sig": sig,
            "proxy_server": server,
            "exit_ip": str(net.get("webrtc:ipv4") or ""),
            "timezone": str(net.get("timezone") or ""),
            "region": str(net.get("locale:region") or ""),
            "language": str(net.get("locale:language") or ""),
            "latitude": net.get("geolocation:latitude"),
            "longitude": net.get("geolocation:longitude"),
            "geoip_applied": bool(net.get("timezone") or net.get("webrtc:ipv4")),
            "proxied": bool(server),
            "source": "上次启动的锁定配置",
        }
    except (OSError, ValueError):
        return {}


def write_network(profile_dir: Path, config_json: str,
                  proxy_sig: str, proxy_server: str) -> dict:
    """runner 每次启动都刷新：即使复用锁定的 config.json，也记录本次 geoip 探测结果。"""
    net = parse_network(config_json)
    if not net:
        return {}
    payload = {
        "detected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "proxy_sig": proxy_sig,
        "proxy_server": proxy_server,
        "exit_ip": str(net.get("webrtc:ipv4") or ""),
        "timezone": str(net.get("timezone") or ""),
        "region": str(net.get("locale:region") or ""),
        "language": str(net.get("locale:language") or ""),
        "latitude": net.get("geolocation:latitude"),
        "longitude": net.get("geolocation:longitude"),
        "geoip_applied": bool(net.get("timezone") or net.get("webrtc:ipv4")),
        "proxied": bool(proxy_server) and proxy_sig != "direct",
    }
    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
        tmp = profile_dir / "network.json.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(profile_dir / "network.json")
    except OSError:
        return {}
    return payload
