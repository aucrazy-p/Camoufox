# 指纹环境管理器

基于 **Camoufox** 的本地多账号隔离环境管理器：一个环境 = 一个独立 profile + 一份锁定的浏览器指纹 + 一条固定代理。只做本地管理，不做云同步、RPA、团队协作。

控制端是 Windows 单文件 exe（FastAPI 本地服务 + Edge WebView2 界面 + 托盘，点 X 隐藏），约 20 MB：浏览器运行时（Playwright / numpy / 指纹模型）与浏览器内核都不打进 exe，首次使用时在界面里手动下载安装，均只装一次。

## 使用

把 exe 放进一个**纯英文路径**的文件夹双击（中文路径会让指纹模型的 C 扩展读不了数据文件），所有数据都在同目录 `CamoufoxData\` 下，拷走整个文件夹就是换电脑。关闭请从托盘「完全退出」，只点 X 是隐藏。

首次使用在「内核与运行时」页按顺序装两样（程序绝不自动下载）：

1. **浏览器运行时**（约 70 MB）：来源为本仓库 Release 固定 tag `runtime-v1`，或「指定本地 zip」离线装
2. **浏览器内核**（约 1.31 GB）：界面内选版本安装，或指定本地 zip

下载失败只提示，不自动尝试第三方镜像；需要代理在界面「下载代理」填一次即可。

## 构建

本机构建（需自备 `runtime\` 内嵌 Python）：

```bat
runtime\python.exe _tools\build.py
```

一次产出 `dist\指纹环境管理器.exe` + `runtime-extra-v1.zip` + `.sha256`，脚本自带白名单与双向断言校验。

**清理临时目录只走 `_tools\purge.py`**（进程内永久删除，不进回收站）：

```bat
runtime\python.exe _tools\purge.py --list   :: 预览
runtime\python.exe _tools\purge.py --temp   :: 清 stage / fresh_data / cache 等
```

构建脚本每次开工会自动清一次。**禁止对项目目录用 `rm -rf` / `Remove-Item` / `rd /s`**：执行环境的安全删除层会把这些批量删除改写成「移到回收站」，反复构建会累积上万条、数 GB，导致回收站显示有文件却清不掉。

## GitHub Actions

`build.yml` **只有 `workflow_dispatch` 手动触发**，无 push / PR / 定时触发。默认不发布；勾选 `publish_release` 时额外做两件事（`--clobber` 覆盖，下载地址固定不变）：

1. 运行时包 → tag `runtime-v1`（程序内置下载地址）
2. exe → tag `app-latest`，资产名固定 **`CamoufoxManager.exe`**（必须 ASCII——中文文件名上传时会被编码损坏，且 `default.exe` 这类名字会被杀软按病毒文件名规则拦截）

## 注意

- 本仓库**公开**。账号数据、cookie、代理凭据、指纹配置一律不入库，`.gitignore` 已按此设置。
- exe 每次启动解压到 `CamoufoxData\tmp\_MEIxxxxxx`，退出即删。
- 时区由 GeoIP 跟随出口 IP；语言由环境设置显式指定，与出口 IP 无关。
