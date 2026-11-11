"""便携版真机验证：exe 放哪数据就跟到哪，且不往 C 盘写。

用法： runtime\\python.exe _tools\\verify_portable.py

做法：把 dist 里的 exe 复制到一个全新目录，按用户实际用法「双击」启动
（CWD = exe 所在目录），核对：
  1. 数据全部落在 <exe 目录>/CamoufoxData
  2. PyInstaller 的 _MEIxxxxxx 解压目录藏在 CamoufoxData/tmp，
     exe 旁边看不到它，也不在系统 %TEMP% / 其它 CWD
  3. WebView2 缓存落在 CamoufoxData/webview2，系统里不出现 pywebview 目录
  4. C 盘旧的 %LOCALAPPDATA%\\CamoufoxManager 完全没被改动
  5. 退出后 _MEI 清理干净、账号数据保留

已知限制（实测）：PyInstaller 的 --runtime-tmpdir 按**启动时的工作目录**解析，
不是按 exe 目录。双击 exe 时 CWD 就是 exe 目录，所以零 C 盘写入成立；
若从 C 盘其它目录用命令行启动，86MB 运行时会临时解压到那个目录（退出即删）。
要连这一条也消除，只能改成 onedir（交付变成一个文件夹）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import purge  # 临时目录永久删除，不进回收站

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "_tools"
EXE = ROOT / "dist" / "指纹环境管理器.exe"
PROBE = TOOLS / "portable_probe"
DATA = PROBE / "CamoufoxData"
CWD_OTHER = TOOLS                      # 对照目录：双击启动时这里不该出现解压目录
LOCAL = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
SYS_TEMP = Path(os.environ.get("TEMP") or "/tmp")
OLD_DATA = LOCAL / "CamoufoxManager"

results: list[bool] = []


def chk(name: str, ok: bool, extra: str = "") -> bool:
    results.append(bool(ok))
    print(f"  {'OK  ' if ok else 'FAIL'} {name:38s} {extra}", flush=True)
    return bool(ok)


def mei(d: Path) -> set[str]:
    try:
        return {p.name for p in d.glob("_MEI*") if p.is_dir()}
    except OSError:
        return set()


def old_state() -> tuple:
    if not OLD_DATA.exists():
        return (False, ())
    items = tuple(sorted(str(p.relative_to(OLD_DATA)) for p in OLD_DATA.rglob("*")))
    return (True, items)


def main() -> int:
    if not EXE.exists():
        print("exe 不存在，请先运行 build.py")
        return 1
    assert PROBE.parent == TOOLS and PROBE.name == "portable_probe"
    purge.rmtree(PROBE)
    PROBE.mkdir(parents=True)
    shutil.copy2(EXE, PROBE / EXE.name)
    portable_exe = PROBE / EXE.name

    before_temp = mei(SYS_TEMP)
    before_cwd = mei(CWD_OTHER)
    pywebview_before = (LOCAL / "pywebview").exists()
    old_before = old_state()

    env = os.environ.copy()
    env.pop("FP_DATA_DIR", None)       # 必须由 exe 自己决定便携目录
    env.pop("FP_FROZEN", None)
    env["NO_PROXY"] = "127.0.0.1,localhost,::1"
    env["no_proxy"] = "127.0.0.1,localhost,::1"

    print(f"=== 启动便携版（模拟双击：CWD = exe 目录 {PROBE.name}） ===", flush=True)
    proc = subprocess.Popen([str(portable_exe)], env=env, cwd=str(PROBE))
    rt_file = DATA / "runtime.json"
    rt = None
    mei_in_tmp = False
    t0 = time.time()
    for _ in range(240):
        mei_in_tmp = mei_in_tmp or bool(mei(DATA / "tmp"))
        if rt_file.exists():
            try:
                cand = json.loads(rt_file.read_text(encoding="utf-8"))
                if cand.get("hwnd"):
                    rt = cand
                    break
            except (ValueError, OSError):
                pass
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    ready = chk("便携版启动就绪", rt is not None, f"耗时 {time.time()-t0:.1f}s")
    if not ready:
        err = DATA / "logs" / "error.log"
        print("错误日志：", err.read_text(encoding="utf-8", errors="replace")[-1500:] if err.exists() else "(无)")
        return 1

    print("=== 1. 解压目录位置（藏进 CamoufoxData/tmp） ===", flush=True)
    chk("_MEI 解压在数据目录 tmp 里", mei_in_tmp, str(sorted(mei(DATA / "tmp"))))
    chk("exe 旁边没有 _MEI 文件夹", not mei(PROBE), str(sorted(mei(PROBE))))
    chk("系统 %TEMP% 无新增 _MEI", not (mei(SYS_TEMP) - before_temp),
        str(sorted(mei(SYS_TEMP) - before_temp)))
    chk("其它目录无 _MEI 残留", not (mei(CWD_OTHER) - before_cwd),
        str(sorted(mei(CWD_OTHER) - before_cwd)))

    print("=== 2. 数据全部落在便携目录 ===", flush=True)
    chk("runtime.json 的 data_dir 正确",
        Path(rt["data_dir"]).resolve() == DATA.resolve(), rt["data_dir"])
    for name in ("profiles", "kernel", "downloads", "logs", "appdata", "webview2", "tmp"):
        chk(f"CamoufoxData/{name} 已建", (DATA / name).is_dir())
    for name in ("app.db", "runtime.json"):
        chk(f"CamoufoxData/{name} 已建", (DATA / name).is_file())

    print("=== 3. WebView2 与临时文件不写 C 盘 ===", flush=True)
    wv = DATA / "webview2"
    # WebView2 是异步建缓存的，窗口出现后还会再写几秒，轮询等它落盘
    wv_files = []
    for _ in range(30):
        wv_files = [p for p in wv.rglob("*") if p.is_file()] if wv.exists() else []
        if wv_files:
            break
        time.sleep(0.5)
    chk("WebView2 缓存落在便携目录", len(wv_files) > 0, f"{len(wv_files)} 个文件")
    chk("系统未出现 pywebview 缓存目录",
        (LOCAL / "pywebview").exists() == pywebview_before,
        f"之前={pywebview_before} 现在={(LOCAL / 'pywebview').exists()}")
    chk("C 盘旧数据目录未被改动", old_state() == old_before,
        f"存在={old_before[0]} 条目={len(old_before[1])}")

    print("=== 4. 服务可用 ===", flush=True)
    B = f"http://127.0.0.1:{rt['port']}"
    H = {"X-FP-Token": rt["token"]}
    try:
        r = requests.get(B + "/api/health", timeout=8)
        chk("health 200", r.status_code == 200)
    except Exception as e:
        chk("health 200", False, str(e))
    try:
        r = requests.get(B + "/api/logs", headers=H, timeout=8)
        path = r.json().get("path", "") if r.status_code == 200 else ""
        chk("日志路径在便携目录", str(DATA / "logs").lower() in str(path).lower(), path)
    except Exception as e:
        chk("日志路径在便携目录", False, str(e))
    try:
        r = requests.get(B + "/api/settings", headers=H, timeout=8)
        got = r.json() if r.status_code == 200 else {}
        # 未改设置时 load_settings() 用内存默认值，不落盘属正常
        chk("设置接口可用（默认不落盘）", got.get("theme") in ("dark", "light", "system"),
            f"theme={got.get('theme')} settings.json={(DATA / 'settings.json').exists()}")
    except Exception as e:
        chk("设置接口可用（默认不落盘）", False, str(e))

    print("=== 5. 退出清理 ===", flush=True)
    try:
        requests.post(B + "/api/shutdown", headers=H, timeout=10)
    except Exception:
        pass
    # bootloader 要删掉 258MB / 3300 个文件，慢盘或杀软扫描时可能要几十秒；
    # 进程退出与目录清理要一起等，否则会误判成残留（脚本提前结束还会被沙箱杀掉进程树）。
    for _ in range(120):
        if proc.poll() is not None and not mei(DATA / "tmp"):
            break
        time.sleep(0.5)
    chk("exe 进程已退出", proc.poll() is not None)
    chk("_MEI 解压目录已清理", not mei(DATA / "tmp"), str(sorted(mei(DATA / "tmp"))))
    chk("runtime.json 已清理", not rt_file.exists())
    chk("账号数据保留", (DATA / "app.db").exists())

    print(f"\n==== {sum(results)}/{len(results)} 通过 ====", flush=True)
    print(f"便携目录：{DATA}", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
