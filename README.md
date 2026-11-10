# 指纹环境管理器

基于 **Camoufox** 的本地多账号隔离环境管理器：一个环境 = 一个独立 profile + 一份锁定的浏览器指纹 + 一条固定代理。只做本地管理，不做云同步、RPA、团队协作。

控制端是一个 Windows 单文件 exe：内嵌 FastAPI 本地服务 + Edge WebView2 界面 + 托盘，点 X 隐藏到托盘。浏览器内核（约 1.31 GB）不含在 exe 里，首次使用时由界面引导下载。

## 成品怎么用

把 `指纹环境管理器.exe` 单独放进一个文件夹（建议纯英文路径，别放 `C:\Program Files`），双击即可。同目录会生成 `CamoufoxData\`，所有数据都在这里：

```
├─ 指纹环境管理器.exe
└─ CamoufoxData\
   ├─ profiles\     账号浏览器数据
   ├─ kernel\       Camoufox 内核与回滚备份
   ├─ downloads\    内核 zip / 断点续传 .part
   ├─ appdata\      GeoIP 等运行时缓存
   ├─ webview2\     WebView2 缓存
   ├─ tmp\          Python/浏览器临时文件（_MEI 解压目录，退出即删）
   ├─ logs\  trash\  app.db  settings.json  runtime.json
```

整个文件夹拷走就是换电脑；换 exe 不丢账号。程序刻意不写 `%LOCALAPPDATA%`。关闭请从托盘「完全退出」，只点 X 是隐藏。

## 源码结构

```
app/          控制端业务代码（FastAPI / 启动器 / 内核管理 / 策略）
  static/     前端页面、样式、脚本
  assets/     应用图标
_tools/       打包引导器、构建脚本、验证脚本、图标生成
resources/    字体清单（裁剪功能已停用）
requirements-build.txt  锁版本的完整构建依赖
.github/workflows/build.yml  手动构建流水线
```

`runtime/`（内嵌 Python 3.13 + 依赖）、`dist/`、`data/`、`CamoufoxData/` 都是**本机产物，不入库**。

## 本地构建

本机构建需要自备 `runtime/`（Windows embeddable Python 3.13 + `requirements-build.txt` 里的依赖）：

```bat
runtime\python.exe _tools\build.py
```

产出 `dist\指纹环境管理器.exe`，脚本会校验包内白名单并断言**没有任何账号数据混入**。

干净的 Windows 机器上可以先重建运行时：

```bat
python _tools\prepare_runtime.py --version 3.13.14
```

这一步会联网下载 python.org 官方 embeddable zip（约 11 MB）与 pip 依赖（约 100 MB）。**不会**下载 Camoufox 内核、fpgen 模型或 GeoIP 库——这些是运行期数据，由成品首次使用时按需获取。

## GitHub Actions 构建

`build.yml` **只有 `workflow_dispatch`**，没有 push / PR / 定时触发：任何构建都必须由人在仓库 Actions 页面点「Run workflow」发起。流程是 setup-python 3.13 → 装构建依赖 → 重建内嵌运行时 → `build.py` 打包 → 把 exe 存为构建产物（保留 90 天）。**不会自动创建 Release 或对外发布**。

单次构建会下载：Python embeddable 约 11 MB、pip 依赖约 100 MB、actions 自带工具。不含 1.31 GB 浏览器内核。

## 注意事项

- 本仓库是**公开仓库**。账号数据、cookie、代理凭据、指纹配置、数据库、日志一律不入库，`.gitignore` 已按此设置；提交前请自行确认。
- 单文件 exe 每次启动都会把内嵌运行时解压到 `CamoufoxData\tmp\_MEIxxxxxx`，退出后删除；这是 onefile 的固有行为，不是首次安装。
- 内核下载失败时程序只提示、不自动尝试第三方镜像；需要代理请在界面的下载设置里填，或用「指定本地 zip」离线安装。
- 时区固定留空，由 GeoIP 跟随出口 IP；语言由环境设置显式指定，与出口 IP 无关。
