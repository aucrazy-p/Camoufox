"""
PyInstaller 引导器（打进 exe 的唯一 Python 代码）。

exe 本身不跑业务逻辑，只负责：
  1. 拿到 _MEIPASS（PyInstaller 把 runtime/ app/ resources/ 解压到这里）
  2. 确定便携数据目录 = exe 所在目录 / CamoufoxData，并把 TEMP/TMP 也指过去
  3. 用内嵌的 pythonw.exe 启动 app/main.py —— 没有控制台窗口
  4. 等它结束（父进程一旦退出，_MEIPASS 会被清理，子进程的文件就没了）

便携版原则：所有业务数据与临时文件都跟 exe 走，绝不往 C 盘写。
目录不可写时直接弹窗退出，不静默回退到 %LOCALAPPDATA%。

CLI 模式额外分配一个控制台，让 JSON 输出可见，并改用 python.exe。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    args = sys.argv[1:]
    is_cli = "--cli" in args

    env = os.environ.copy()
    env["FP_FROZEN"] = "1"
    env["FP_APP_DIR"] = str(base)
    env["FP_EXE_PATH"] = sys.executable
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"  # CLI 模式用 os._exit 收尾，输出必须无缓冲
    # 不碰 LOCALAPPDATA：上一版改了它，Edge 把 619MB 用户配置写进了项目目录

    # ---- 便携数据根：exe 放哪，数据就跟到哪 ----
    # 外部已指定 FP_DATA_DIR 时尊重它（端到端验证脚本用干净目录）。
    exe_dir = Path(sys.executable).resolve().parent
    data_dir = Path(env.get("FP_DATA_DIR") or (exe_dir / "CamoufoxData")).resolve()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        try:
            probe.unlink()
        except PermissionError:
            # 杀软实时扫描可能短暂占住刚创建的文件导致删除被拒；
            # 写入成功已证明目录可写，残留文件下次启动覆盖，不因此拦启动
            pass
        tmp_dir = data_dir / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        try:
            ctypes.windll.user32.MessageBoxW(
                0,
                f"数据目录不可写，便携版无法启动：\n{data_dir}\n\n{exc}\n\n"
                "请把整个文件夹移到有写入权限的磁盘（不要放 C:\\Program Files）。",
                "指纹环境管理器", 0x10,
            )
        except Exception:
            pass
        return 2
    env["FP_DATA_DIR"] = str(data_dir)
    # Python / Playwright / WebView2 的临时文件一律落便携目录，不走系统 %TEMP%
    for key in ("TEMP", "TMP", "TMPDIR"):
        env[key] = str(tmp_dir)

    if is_cli:
        try:
            ctypes.windll.kernel32.AllocConsole()
        except Exception:
            pass
        py = base / "runtime" / "python.exe"
        flags = 0
    else:
        py = base / "runtime" / "pythonw.exe"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    script = base / "app" / "main.py"
    if not py.exists() or not script.exists():
        try:
            ctypes.windll.user32.MessageBoxW(
                0,
                f"程序文件不完整。\n缺少：{py if not py.exists() else script}",
                "指纹环境管理器", 0x10,
            )
        except Exception:
            pass
        return 2

    proc = subprocess.Popen(
        [str(py), str(script), *args],
        cwd=str(base), env=env, creationflags=flags,
    )
    return proc.wait()


if __name__ == "__main__":
    sys.exit(main())
