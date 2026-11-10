"""
为干净 Windows 机器 / GitHub Actions 重建 runtime/（内嵌 Python + 依赖）。

本机开发用的 runtime/ 是手工搭的 Python 3.13 embeddable + site-packages，
体积约 300 MB，属于构建产物，不进 Git。CI 上用它按同一结构重建：

    python _tools/prepare_runtime.py --version 3.13.14

会联网下载（只有本脚本被显式运行时才发生，程序自身不受影响）：
  1. python.org 官方 Windows embeddable zip   约 11 MB
  2. requirements-build.txt 里的 pip 依赖     约 100 MB

不会下载：Camoufox 内核、fpgen 模型、GeoIP 库 —— 这些都是运行期数据，
由成品 exe 在首次使用时按需获取，与本机行为一致。
"""
from __future__ import annotations

import argparse
import io
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "runtime"
REQS = ROOT / "requirements-build.txt"


def fetch_embed(version: str) -> bytes:
    url = (
        f"https://www.python.org/ftp/python/{version}/"
        f"python-{version}-embed-amd64.zip"
    )
    print(f"下载官方 Python embeddable（约 11 MB）:\n  {url}", flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": "CamoufoxManager-CI"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.read()


def minor_of(version: str) -> int:
    parts = version.split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise ValueError(f"版本号格式不对: {version!r}（应形如 3.13.14）")
    return int(parts[1])


def main() -> int:
    ap = argparse.ArgumentParser(description="重建内嵌 Python 运行时")
    ap.add_argument("--version", default="3.13.14",
                    help="Python 版本，需与 setup-python 的主次版本一致（默认 3.13.14）")
    args = ap.parse_args()
    version = args.version.strip()
    minor = minor_of(version)

    if not REQS.is_file():
        raise RuntimeError(f"缺少依赖清单: {REQS}")

    data = fetch_embed(version)
    if RUNTIME.exists():
        shutil.rmtree(RUNTIME, ignore_errors=True)
    RUNTIME.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(RUNTIME)

    # embeddable 自带的 _pth 默认不启用 site-packages；覆写成本机验证过的那一份
    pth = RUNTIME / f"python3{minor}._pth"
    if not pth.exists():
        found = sorted(RUNTIME.glob("python3*._pth"))
        if not found:
            raise RuntimeError("embeddable 包里找不到 _pth 文件")
        pth = found[0]
    pth.write_text(
        f"python3{minor}.zip\n.\nLib\\site-packages\n\nimport site\n",
        encoding="utf-8",
    )

    target = RUNTIME / "Lib" / "site-packages"
    target.mkdir(parents=True, exist_ok=True)
    print("安装依赖到 runtime/Lib/site-packages …", flush=True)
    subprocess.check_call([
        sys.executable, "-m", "pip", "install",
        "--disable-pip-version-check", "--no-warn-script-location",
        "--target", str(target), "-r", str(REQS),
    ])

    for name in ("python.exe", "pythonw.exe", f"python3{minor}.dll",
                 f"python3{minor}.zip", f"python3{minor}._pth"):
        if not (RUNTIME / name).is_file():
            raise RuntimeError(f"runtime 缺少 {name}")

    probe = (
        "import fastapi, uvicorn, camoufox, playwright, requests, "
        "pywebview, pystray, PIL, numpy; print('runtime import OK')"
    )
    subprocess.check_call([str(RUNTIME / "python.exe"), "-c", probe])
    print(f"runtime 准备完成: {RUNTIME}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("准备 runtime 失败:", type(exc).__name__, exc, flush=True)
        sys.exit(1)
