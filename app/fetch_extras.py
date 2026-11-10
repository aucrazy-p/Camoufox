"""
补充组件下载器：Camoufox 除浏览器内核外，还有两个运行时需要联网拉取的组件。

  1. fpgen 指纹模型      约 1.5 MB   装到 runtime/Lib/site-packages/fpgen/data
  2. GeoIP AIO 数据库    约 50 MB    装到 data/appdata/camoufox/Cache/geoip

两者都托管在 GitHub Releases。默认直连（旧的 127.0.0.1:3067 代理早已失效），
确实需要代理时用环境变量 FP_PROXY 指定，例如 http://127.0.0.1:7890。
不会自动尝试任何第三方加速镜像。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

common.prepare_env()


def _use_proxy(proxy: str | None) -> None:
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        if proxy:
            os.environ[k] = proxy
        else:
            os.environ.pop(k, None)


def _step(name: str, func) -> bool:
    print(f"\n---- {name} ----", flush=True)
    try:
        func()
        print(f"{name}: 完成", flush=True)
        return True
    except Exception as exc:
        print(f"{name}: 失败 -> {type(exc).__name__}: {exc}", flush=True)
        return False


def main() -> None:
    proxy = os.environ.get("FP_PROXY", "").strip()
    print(f"环境目录: {common.ROOT}")
    print(f"代理: {proxy or '不使用'}")

    def do_fpgen() -> None:
        from camoufox.fpgen_model import ensure_fpgen_model

        ensure_fpgen_model()

    def do_geoip() -> None:
        from camoufox.geolocation import download_mmdb

        download_mmdb()

    ok = True
    for step_name, func, done in (
        ("fpgen 指纹模型", do_fpgen, common.fpgen_ready),
        ("GeoIP 数据库", do_geoip, common.geoip_ready),
    ):
        if done():
            print(f"\n---- {step_name} ----\n已存在，跳过", flush=True)
            continue
        _use_proxy(proxy or None)
        if _step(step_name, func) and done():
            continue
        # 代理不通就退回直连再试一次
        _use_proxy(None)
        if _step(step_name + "（直连重试）", func) and done():
            continue
        ok = False

    print("\n===== 结果 =====")
    print(f"浏览器内核 : {'就位 ' + common.kernel_exe().parent.name if common.kernel_ready() else '缺失'}")
    print(f"指纹模型   : {'就位' if common.fpgen_ready() else '缺失'}")
    print(f"GeoIP 库   : {'就位' if common.geoip_ready() else '缺失（不影响启动，时区不自动对齐）'}")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
