# EverSend 韧传 —— 便携版（绿色版）打包说明

本文档说明如何用 `tools/` 里的三个脚本构建、校验 EverSend 的**纯绿色便携包**：
用户下载一个 `.zip`，解压（哪怕解压到 U 盘）后双击启动器即可运行，
**不需要安装 Python、不需要 pip、不写注册表、不写 `%APPDATA%`、不写家目录**。

---

## 1. 需求与结论

| 要求 | 实现方式 | 验证手段 |
| --- | --- | --- |
| 解压即用，不装任何东西 | 内置 CPython（python-build-standalone `install_only`，天然可重定位）+ 所有 wheel 解压进 `site/` | `verify_green.py` 用 `env -i`（只给 `HOME`/`PATH`/`TMPDIR`）运行成品 |
| 解压到 U 盘也能跑 | 所有路径都从启动器自身位置推导（`$0` / `%~dp0`），无绝对路径 | 把成品复制到含**空格和中文**的目录、再**移动**到第二个位置后重跑全部检查 |
| 写保护 U 盘也能启动 | 启动器真实写文件探测可写性，失败则把数据目录切到临时目录并提示 | `chmod -R a-w` 后跑 `--selftest` / 传输测试，且校验树内**零新增文件** |
| 不污染宿主机 | `XDG_CONFIG_HOME`/`XDG_CACHE_HOME`/`XDG_DATA_HOME` 指向包内 `data/`；`PYTHONNOUSERSITE=1` | 断言假的 `$HOME` 在全部测试后**仍然为空** |
| 传输功能完好 | 成品自己跑一次局域网回环传输并比对 sha256 | `run.sh --selftest-transfer`（调用应用自带的 `cli selftest`） |

实测：Linux 成品在 `env -i` + 中文空格路径 + 移动后 + 只读挂载下，
`verify_green.py` **33 项检查全部 PASS，0 FAIL**。

---

## 2. 构建流程总览

```
tools/fetch_runtime.py    下载并解包「内置解释器 + 全部 wheel」到 tools/.cache/stage-<平台>/
        ↓
tools/build_green.py      组装 EverSend/ 目录树 → 编译字节码 →（可选 --strip）→ 打 zip
        ↓
tools/verify_green.py     把成品当成「别人电脑上的 U 盘」来跑，端到端验证
```

三个脚本都只用 Python 标准库（`urllib.request`，不用 `requests`），
都支持 `--help`。**构建过程不需要系统装 pip**：wheel 本身就是 zip，
解压到 `site/` 就是安装（见 §4）。

---

## 3. 构建命令

### 3.1 Linux x86_64（可在本机完整构建并验证）

```bash
cd eversend

# 1) 下载内置解释器与 wheel（首次约 110 MiB，缓存在 tools/.cache/）
python3 tools/fetch_runtime.py --platform linux --out tools/.cache/stage-linux

#    如果 github.com 被墙（api.github.com 能通但 release 下载 0 字节）：
python3 tools/fetch_runtime.py --platform linux --out tools/.cache/stage-linux --source mirror

#    PyPI 的「项目级 JSON」对 cffi 这类发布极多的项目非常慢（实测 3 分钟以上），
#    可以直接钉住版本跳过它：
python3 tools/fetch_runtime.py --platform linux --out tools/.cache/stage-linux \
        --source mirror --pin cffi=2.0.0

# 2) 组装 + 打包
python3 tools/build_green.py --platform linux --out tools/.build

# 3) 端到端验证（成品 zip 或解压后的目录都可以）
python3 tools/verify_green.py tools/.build/EverSend-1.0.0-linux.zip
```

### 3.2 Windows x64（可在 Linux 上下载/组装，但**不能执行**）

```bash
python3 tools/fetch_runtime.py --platform windows --out tools/.cache/stage-windows --source mirror --pin cffi=2.0.0
python3 tools/build_green.py  --platform windows --out tools/.build
python3 tools/verify_green.py tools/.build/EverSend-1.0.0-windows.zip --platform windows
```

Windows 目标在 Linux 上只能做**结构验证**（文件齐全、名字正确、zip 内容、
启动器引用路径存在），因为 `python.exe` 在 Linux 上跑不起来。真正的运行验证
交给 CI 里 `windows-latest` 上的 `portable-windows` job，它把成品解压到
「我的 U 盘」这样一个带空格和中文的目录，再用**包里自带的** `python.exe`
执行 `tools/ci_windows_green.py`（内置解释器与内置 wheel、AES-256-GCM 加解密、
24 MiB 真传输逐字节比对、内置 Qt 建窗口并截图），最后再跑一次 `--cli selftest`
——也就是 `run.bat` 走的那条路。见 §9。

### 3.3 可选：裁剪体积

```bash
python3 tools/build_green.py --platform linux --out tools/.build-strip --strip
```

`--strip` 会删掉 QtWidgets 应用永远用不到的东西，并在裁剪后**重新导入
QtCore/QtGui/QtWidgets 建一个 offscreen 窗口**确认没被删坏；万一删坏了，
构建直接失败并提示「去掉 --strip 重新构建」。默认**不开**，因为桌面端 UI
目前还在开发中，万一将来改成 QML 界面，QML 那一块就不能删（见 §7）。

### 3.4 其他常用参数

| 参数 | 作用 |
| --- | --- |
| `--tag` / `--python-version` | 换 python-build-standalone 的 release tag / CPython 版本（默认 `20260901` / `3.14.7`） |
| `--pin 项目=版本` | 钉住任意（含传递依赖）版本，跳过 PyPI 慢接口；可重复 |
| `--wheels A,B` | 换掉要打包的顶层项目（默认 `PySide6-Essentials,cryptography`） |
| `--pyside-version` / `--cryptography-version` | 钉住顶层项目版本 |
| `--offline` | 只用缓存，不联网（可配合手工放进 `tools/.cache/downloads/` 的文件） |
| `--force` | 忽略缓存重新下载 |
| `--symlinks preserve` | wheel 内的符号链接保留为符号链接（**不推荐**，见 §5） |
| `--drop-site-sources` | 编译字节码后删掉 `site/**/*.py`（实测只省 1.1~1.2 MiB，默认不删） |
| `--no-zip` | 只组装目录树，不打 zip |
| `--compress-level 0..9` | zip 压缩级别（默认 6） |
| `verify_green.py --quick` | 跳过传输与只读两项（快速冒烟） |
| `verify_green.py --work-dir DIR` | 指定临时工作目录（**默认在 `TMPDIR`，需要约 1.5 GB 空余**） |

---

## 4. 为什么「解压 wheel」就等于「安装」

wheel 就是一个 zip，二进制 wheel 里的 `.so`/`.pyd` 已经放在最终的导入路径上，
所以解压到 `site-packages` 根目录即可。但有三个坑，`fetch_runtime.py` 都处理了：

1. **`.data` 目录重映射**：`xxx.data/purelib/...`、`xxx.data/platlib/...` 属于
   site-packages 根目录，不能留在子目录里；`scripts`/`data`/`headers` 是构建机
   才关心的东西（命令行脚本、man page），直接跳过。
2. **符号链接条目**：wheel 允许把符号链接存成「external_attr 里带 `S_IFLNK`
   的 zip 条目」。`zipfile.extractall()` 会把这种条目写成一个**内容是链接目标的
   文本文件**，之后加载动态库必然失败。本项目默认把链接**实体化**（复制目标
   内容），因为成品要去 FAT32/exFAT U 盘，那里根本没有符号链接。
3. **信任问题**：每解压一个文件都会算 sha256，并和 wheel 自带的 `RECORD`
   逐条比对（`RECORD` 用的是无填充的 URL-safe base64，不是十六进制）。
   本次构建 2677 个文件中 **2672 个通过 RECORD 校验**，剩下 5 个是 `RECORD`
   自身（它对自己的条目没有哈希）。任何一个文件对不上，构建直接报错。

**没有**用内嵌解释器的 `pip`，也不需要在构建机装 pip —— 上面的校验比
`pip install --target` 更严格（pip 默认不核对 RECORD 的每个文件）。

### 4.1 依赖是怎么解析的

不靠人工维护清单，而是**读每个 wheel 自己的 `METADATA`**：

* `PySide6-Essentials 6.11.2` → `Requires-Dist: shiboken6==6.11.2` → 精确锁定同版本；
* `cryptography 50.0.1` → `cffi>=1.14; platform_python_implementation != "PyPy"`
  → 解析；`typing-extensions>=4.13.2; python_full_version < '3.11'` 与
  `bcrypt>=3.1.5; extra == 'ssh'` 被**按目标平台的 PEP 508 marker 正确跳过**；
* `cffi 2.0.0` → `pycparser` → 纯 Python wheel。

marker 是按**目标平台**（不是构建机）求值的：`sys_platform`、`platform_system`、
`platform_machine`、`python_version` 等取自目标平台描述表，所以给 Windows 打包时
不会把 Linux 专属依赖拖进来。

---

## 5. 成品目录结构

```
EverSend/
  run.sh                  Linux/macOS 启动器（zip 里保留 0755 可执行位）
  run.bat                 Windows 启动器（UTF-8 + chcp 65001，CRLF 换行）
  README.txt              首次使用说明（中文，UTF-8 **带 BOM**，保证记事本不乱码）
  runtime/                内置 CPython 3.14.7（python-build-standalone install_only_stripped）
  app/eversend/          应用源码（故意保留 .py，方便查看和魔改）
  app/eversend_green.py  打包工具链提供的入口（见 §6.1）
  site/                   全部依赖，site-packages 布局
  data/                   运行期产生：配置、身份密钥、日志（首次运行后才有内容）
  received/               默认接收目录
```

### 5.1 符号链接与绝对路径（两条硬性规则）

* **成品树里没有任何符号链接**。内嵌解释器自带的 1039 个链接被实体化成真实文件
  （+31.8 MiB，主要是 `bin/python3` 和 terminfo）；其中 8 个**只有开发才用**的链接
  （`bin/python`、`bin/idle3`、`bin/pydoc3`、`lib/libpython3.14.so`、pkgconfig 等）
  直接删除——它们的目标都以真名存在，而实体化它们要再多复制两个 32 MiB 的大文件。
  `runtime/bin/python3` 本身**保留为真实文件**，因为那是启动器首选路径。
* **没有任何构建机的绝对路径**。`verify_green.py` 会扫描成品里 7449 个文件，
  确认构建目录、当前工作目录、`/home/xgl/python` 都不出现（字节级搜索，包含 `.pyc`）。
  字节码用 `compileall -b --invalidation-mode unchecked-hash` 编译，
  文件名以相对路径记录，且**换机器/换 mtime 不会失效**——U 盘上解压出来的
  文件时间戳必然变，如果用默认的时间戳校验模式，Python 每次启动都会试图重写
  `.pyc`（只读介质上写不了，就退化成每次重新解析源码）。

### 5.2 关于 `site/**/*.py`

默认**保留** `site/` 里的 `.py` 源码（同时保留了编译好的 `.pyc`）。实测删除它们
只能省 **1.1~1.2 MiB**（PySide6 的体量几乎全在 `.so`/`.dll`/`.pyi` 上），
而删掉源码有破坏「某个包用 `__file__` 找数据文件」这类边角情况的风险，
不划算。需要的话用 `--drop-site-sources` 显式打开。

---

## 6. 启动器：路径、环境、只读回退

### 6.1 启动器怎么找到「自己」

* `run.sh`：纯 POSIX sh。先沿 `$0` 逐级 `readlink` 跟随符号链接（最多 40 层），
  再 `cd -P "$(dirname "$SELF")"` 取**物理路径**，最后 `cd` 进去。
  所以「从别的目录调用」「通过符号链接调用」「路径里有空格和中文」都成立。
  实测路径：`.../U 盘 测 试/解压 后的 目录/EverSend`。
* `run.bat`：用 `%~dp0`（脚本自身目录，末尾反斜杠会被去掉），`cd /d` 进去，
  完全不用 `%CD%`。

### 6.2 环境变量（只指向包内部）

```
PYTHONHOME   = <包>/runtime
PYTHONPATH   = <包>/app;<包>/site
PYTHONDONTWRITEBYTECODE = 1      # 只读介质上绝不尝试写 .pyc
PYTHONUTF8   = 1                 # 中文/非 ASCII 路径
PYTHONNOUSERSITE = 1             # 不加载用户级 site-packages
PYTHONUNBUFFERED = 1             # --cli 重定向到管道/文件时也能立刻看到输出
EVERSEND_HOME = <包>（只读时 = 临时数据目录）
XDG_CONFIG_HOME / XDG_CACHE_HOME / XDG_DATA_HOME = <数据目录>/xdg-*
QT_PLUGIN_PATH / QT_QPA_PLATFORM_PLUGIN_PATH      = 探测后设置（见下）
LD_LIBRARY_PATH（Linux）= <包>/runtime/lib : <Qt 库目录>
```

`EVERSEND_HOME` 是应用本身（`eversend.desktop.app` 与 `eversend.cli`）读取的
「绿色包的家」。**必须由启动器设置**：否则应用会退回 `os.getcwd()`，
从别的目录启动就会把数据写到错误的位置。只读介质上它被指向临时目录，
这样应用自己的回退逻辑和启动器的回退逻辑结果一致。

### 6.3 Qt 插件路径：两种 wheel 布局都要照顾

PySide6 的 wheel 布局**两个平台不一样**：

| 平台 | Qt 库 | Qt 插件 |
| --- | --- | --- |
| Linux/macOS | `site/PySide6/Qt/lib` | `site/PySide6/Qt/plugins` |
| Windows | `site/PySide6/*.dll` | `site/PySide6/plugins` |

启动器先探测再设置，**并且只有目录真的存在时才覆盖** Qt 的查找路径：

```sh
QTPLUGINS="$APPDIR/site/PySide6/Qt/plugins"
[ -d "$QTPLUGINS" ] || QTPLUGINS="$APPDIR/site/PySide6/plugins"
if [ -d "$QTPLUGINS/platforms" ]; then
    export QT_PLUGIN_PATH="$QTPLUGINS"
    export QT_QPA_PLATFORM_PLUGIN_PATH="$QTPLUGINS/platforms"
fi
```

这一点是 `verify_green.py` 的「启动器引用路径必须存在」检查**实际抓到的 bug**：
最初的 Windows 版把 `QT_QPA_PLATFORM_PLUGIN_PATH` 指向了 Linux 才有的
`PySide6\Qt\plugins\platforms`。这个环境变量一旦指向不存在的目录，
Qt 会**直接找不到 `qwindows.dll` 而无法创建窗口**（不是「退化成默认值」）。

**不硬编码 `xcb`**：`QT_QPA_PLATFORM` 保持不设置，让 Qt 自己选 xcb/wayland，
用户也可以自己覆盖。没有图形会话时（`DISPLAY` 与 `WAYLAND_DISPLAY` 都为空）
启动器打印中文错误并给出 `--cli` 备选，退出码 3：

```
错误：没有检测到图形界面（DISPLAY 与 WAYLAND_DISPLAY 都没有设置）。
      EverSend 的窗口需要图形会话。可以：
        1) 在桌面环境（X11 或 Wayland）里双击/运行本程序；
        2) 使用命令行模式：  ./run.sh --cli
        3) 只做自检：        ./run.sh --selftest
```

### 6.4 只读介质回退

启动器**真的去写一个临时文件**来判断可写性（`-w` 位在只读挂载/root 下不可靠）：

```sh
probe_write() { _dir="$1"; [ -d "$_dir" ] || mkdir -p "$_dir" || return 1
                _t="$_dir/.eversend-write-test.$$"
                ( umask 077; : > "$_t" ) 2>/dev/null || return 1
                rm -f "$_t"; return 0; }
```

* 可写 → `data/`、`received/` 就在包内，`EVERSEND_READONLY=0`。
* 不可写 → 数据目录切到 `${TMPDIR:-/tmp}/eversend-<uid>`（Windows 用
  `%TEMP%\eversend`），并且**把 `EVERSEND_HOME` 一起指过去**，
  接收目录随之变成 `<临时目录>/received`。程序照常收发文件，
  启动时用中文明确告诉用户数据改存到哪里了。
* 用户显式设置了 `EVERSEND_DATA_DIR` / `EVERSEND_RECEIVE_DIR` 时一切照旧，
  启动器不覆盖。

`PYTHONDONTWRITEBYTECODE=1` + `-B` 保证只读树里连一个 `.pyc` 都不会被写；
验证脚本会在只读测试前后对整棵树做 (大小, mtime) 快照对比，确认**零新增、零改动**。

### 6.5 Windows 启动器的取舍

`run.bat` 永远会带出一个控制台窗口，所以：

* **图形模式**：先用 `python.exe -c "pass"` 做一次极快的自检（确认内嵌解释器能起来），
  然后 `start "" pythonw.exe ...` 拉起无控制台的 GUI，`.bat` 立即退出、
  控制台窗口随之关闭，不会留下黑窗口。
* **出错时**：`pythonw.exe` 没有 stdout，所以入口脚本会把 traceback 写进
  `data\logs\eversend.log`，并用 `MessageBoxW`（`ctypes`，只用内嵌解释器就够）
  弹中文错误框；启动器自身的前置错误（找不到 `python.exe`、目录进不去等）
  直接 `echo` 后 `pause`，双击的用户能看到。
* **`--cli` 模式**：直接在现有控制台里跑 `python.exe`，输出可见、退出码透传。
* 文本编码：`run.bat` 存为 UTF-8 并在开头 `chcp 65001`；`README.txt` 存为
  **UTF-8 with BOM**（否则老版本记事本按 ANSI 解读会乱码）。

### 6.6 入口脚本 `app/eversend_green.py`

它在 `tools/green_main.py`，由 `build_green.py` 复制进成品。职责很小：

* 默认 → `eversend.desktop.app.main()`（应用自己的 Qt 引导）；
* `--cli` → `eversend.cli.main()`（应用自带 `serve`/`discover`/`send`/`selftest`，
  无子命令时默认 `serve`，所以 `./run.sh --cli` 直接可用）；
* `--selftest` → 工具链自带的快速环境自检（版本、PySide6、cryptography 真实调用
  Ed25519/X25519/ChaCha20-Poly1305、TCP/UDP 端口能否绑定）；
* `--selftest-transfer` → 优先调用应用自带的 `cli selftest`（引擎 + 3 MiB 回环
  传输 + 完整性比对），失败则退回工具链自己的进程内传输自检；
* `--print-paths` → 打印解释器、`app home`、数据目录、是否只读（验证脚本用它）。

> 注意：入口脚本放在 `tools/` 是打包工具链的所有权边界。`src/` 里没有
> `__main__.py` 时它也能工作；现在应用已经有了自己的入口（`eversend/__main__.py`
> 和 `eversend/cli.py`），`green_main.py` 只做转发，不重复实现应用逻辑。

**端口自检的语义**：绑定失败若是 `EADDRINUSE`（端口被别的 EverSend 实例占用），
只报 **warning** 不算失败——「开发者忘了关程序」不应该让构建失败；
其它错误（如权限）仍然算失败。

---

## 7. `--strip` 到底删了什么

Qt 的体量在 `site/PySide6/Qt/`（Linux 实测 176.2 MiB，Windows 类似）。
`--strip` 只删 QtWidgets 应用**永远到不了**的部分：

| 类别 | 内容 | 为什么安全 |
| --- | --- | --- |
| 类型存根 | `**/*.pyi`（4.6 MiB） | 只有 IDE/类型检查器读，运行时不导入 |
| QML / Qt Quick | `Qt/qml`（16.9 MiB）、`Qt/metatypes`（7.0 MiB）、`Qt/libexec`、`libQt6Quick*`/`libQt6Qml*`/`Labs`/`Lottie`、`QtQuick*.abi3.so`、`qmlls`/`qmlformat`/`qmllint` 等 | 桌面端是 QtWidgets 应用，不加载 QML 引擎 |
| Qt 开发工具 | `assistant`/`designer`/`linguist`/`lupdate`/`lrelease`、`libQt6Designer*`、`libQt6UiTools`/`Help`/`Sql`/`Test`、`plugins/{designer,sqldrivers,help,qmltooling,qmllint}` | 它们是独立程序，与本应用无关 |
| Qt 翻译 | 除 `*_zh_CN.qm`/`*_zh_TW.qm` 外的 ~40 种语言（约 12 MiB） | 保留中文，界面里 Qt 自带对话框仍是中文 |
| 解释器里的开发件 | `include/`、`lib/pkgconfig`、`share/man`、`share/terminfo`、Tcl/Tk（`lib/tcl9*`、`tk9.0`、`itcl*`、`thread*`）、`bin/pip*`、`bin/idle3*`、`bin/pydoc3*`、`bin/*-config` | 应用不用 tkinter/curses/pip，也不需要编译扩展 |

**绝不会删**（写死在 `STRIP_PROTECTED` 里，任何规则都动不了）：

* `plugins/platforms/**`：`qwindows.dll` / `libqxcb.so` / `libqwayland*.so`
  —— 少了任何一个，对应平台上窗口就起不来；
* `plugins/xcbglintegrations/**`、`plugins/imageformats/**`、`plugins/iconengines/**`、
  `plugins/styles/**`、`plugins/platformthemes/**`（原生文件对话框外观）、
  `plugins/networkinformation/**`（发现设备要枚举网卡）、`plugins/tls/**`、
  `plugins/platforminputcontexts/**`；
* `libQt6Core/Gui/Widgets/Network/Svg/DBus/PrintSupport`、`libEGL*`、`libGL*`、
  `libxcb*`、ICU（`libicudata` 一个就 30.5 MiB，但 Qt6Core 直接链接它）、
  `libQt6XcbQpa`，以及 Windows 侧的 `Qt6*.dll`、`msvcp140*.dll`、`vcruntime*`、
  `opengl32sw.dll`、`d3dcompiler_47.dll`。

裁剪后构建会**重新导入 QtCore/QtGui/QtWidgets 并建一个 offscreen 窗口**；
失败即停止构建。即便如此，默认仍然不开启 `--strip`：
万一将来桌面端改成 QML，`Qt/qml` 与 Quick 那 60+ MiB 就不能删了。

---

## 8. 实测体积（真实构建产物）

构建环境：Arch Linux，x86_64，glibc 2.44，Python 3.14.7。

### 8.1 依赖清单与压缩包大小

| 组件 | 版本 | 平台 | wheel/tar 大小 | 解压后 |
| --- | --- | --- | --- | --- |
| CPython（`install_only_stripped`，tag `20260901`） | 3.14.7 | linux x86_64 | 34.3 MiB | 96.5 MiB |
| CPython（同上） | 3.14.7 | windows x86_64 | 21.7 MiB | 57.9 MiB |
| PySide6-Essentials | 6.11.2 | linux（`cp310-abi3-manylinux_2_34_x86_64`） | 76.4 MiB | 225.2 MiB |
| PySide6-Essentials | 6.11.2 | windows（`cp310-abi3-win_amd64`） | 73.3 MiB | 201.6 MiB |
| shiboken6（PySide6 传递依赖，版本必须相同） | 6.11.2 | linux / windows | 0.3 / 1.2 MiB | 0.6 / 3.0 MiB |
| cryptography | 50.0.1 | linux / windows | 4.5 / 3.7 MiB | 14.3 / 10.0 MiB |
| cffi（cryptography 依赖） | 2.0.0 | linux / windows | 0.2 / 0.2 MiB | 0.7 / 0.5 MiB |
| pycparser（cffi 依赖） | 3.0 | 纯 Python | 0.05 MiB | 0.2 MiB |

下载总量：Linux 约 116 MiB，Windows 约 103 MiB（都已缓存在 `tools/.cache/`）。

### 8.2 成品大小（`--strip` 关闭，默认配置）

| 目录 | Linux | Windows |
| --- | --- | --- |
| `runtime/` | 128.2 MiB | 48.2 MiB |
| `site/` | 242.7 MiB | 216.9 MiB |
| `app/`（源码 + `.pyc`） | 1.3 MiB | 1.3 MiB |
| 启动器 + README | ~15 KiB | ~14 KiB |
| **解压后合计** | **372.2 MiB** | **266.4 MiB** |
| **发布 zip** | **130.5 MiB**（136,856,507 B） | **97.7 MiB**（102,472,283 B） |

补充数字：

* 字节码编译前 369.8 MiB → 编译后 372.2 MiB（多出 2.4 MiB，换来启动时不用重新编译）；
* 符号链接实体化 +31.8 MiB（Linux）；
* `site/` 里 1.2 MiB 是 `.py` 源码，删掉只省这么多，所以默认保留；
* **Qt 是绝对大头**：Linux 的 `site/PySide6/Qt` 就有 176.2 MiB，
  其中 `libicudata.so.73` 30.6 MiB、`libQt6Gui` 10.9 MiB、Quick/Qml 系列约 40 MiB、
  QML 数据 16.9 MiB、翻译 13.1 MiB、插件 9.5 MiB、类型信息 7.0 MiB。

也就是说：**PySide6-Essentials + Qt 确实非常占地方**——它是整个 zip 的
四分之三（Linux 上 242.7 / 372.2 MiB）。`--strip` 实测能把 QML/Quick、Qt 开发工具、
`.pyi`、非中文翻译和解释器里的开发件去掉：

| | 默认构建 | `--strip` 构建 | 差值 |
| --- | --- | --- | --- |
| `runtime/` | 128.2 MiB | 119.8 MiB | −8.4 MiB |
| `site/` | 242.7 MiB | 124.0 MiB | **−118.7 MiB** |
| **解压后合计** | **372.2 MiB** | **245.1 MiB** | **−127.2 MiB（−34%）** |
| **发布 zip** | **130.5 MiB** | **88.7 MiB** | **−41.8 MiB（−32%）** |

裁剪共删除 375 个条目（每次构建都会逐条打印文件名、大小和原因），
之后重新导入 QtCore/QtGui/QtWidgets 并建 offscreen 窗口仍然 `rc=0 qt ok`，
裁剪后的成品也通过了 25 项验证（含中文空格路径、移动、符号链接调用、真实回环传输）。
代价是「将来改成 QML 界面就不能用」。真要极致压体积，只能换成
PyQt6/PySide6 的裁剪版或者干脆不用 Qt（改用系统 WebView / TUI），
但那已经不是打包工具链能决定的事了。

### 8.3 三个产物的校验和

| 产物 | 大小 | SHA-256 |
| --- | --- | --- |
| `EverSend-1.0.0-linux.zip` | 136,856,506 B (130.5 MiB) | `f5174d080945d9df15275eaa4356e191f3af90c3b5de5cc055a98285e9d62bf2` |
| `EverSend-1.0.0-windows.zip` | 102,472,972 B (97.7 MiB) | `1533819702b0226ae98a81033953578bf5cee51cfadcda303a0a8feee05b89e8` |
| `EverSend-1.0.0-linux.zip`（`--strip`） | 93,004,912 B (88.7 MiB) | `cd7e4a8284acb991ddac8fe7b2904fbaff66042204527307d33a965b2324c460` |

### 8.4 最终验证结果

```
33 passed, 0 failed, 0 skipped   (15.3s)     # Linux 成品（含 zip 解压、移动、只读、$HOME 零污染）
25 passed, 0 failed, 0 skipped   (7.0s)      # Linux --strip 成品（--quick）
19 passed, 0 failed, 1 skipped   (2.1s)      # Windows 成品（结构检查；「执行」那项由 CI 接手）
RESULT: PASS
```

Windows 成品的「执行」不在上面这 19 项里，它由 `ci.yml` 的 `portable-windows`
job 在真 Windows runner 上完成（Wine 下也手工验证过）：

```
[1] 内置解释器与内置依赖        5/5  PASS   （解释器与 eversend 包都在包内，AES-256-GCM 可用）
[2] 包里的代码真传一个文件       4/4  PASS   （24 MiB，SHA-256 逐字节一致，83 MiB/s）
[3] 内置 Qt 真的能画出窗口       2/2  PASS   （PySide6 6.11.2，截图已保存）
--cli selftest                  PASS        （run.bat 走的那条路）
```

---

## 9. 已知限制 / 没验证到的地方（重要）

1. **Windows 成品的运行验证靠 CI，作者本机没有 Windows**。`ci.yml` 的
   `portable-windows` job 在真 `windows-latest` 上组装并解压成品，用包里自带的
   解释器跑 `tools/ci_windows_green.py`：内置解释器与内置 `eversend` 包、
   内置 `cryptography`（AES-256-GCM 真加解密）、24 MiB 真传输逐字节比对、
   内置 Qt 建窗口并截图，最后跑一遍 `--cli selftest`。同一套在 Wine 下也手工跑过
   （24 MiB / 83 MiB·s⁻¹）。**仍然没有验证的**：
   `run.bat` 在真的 cmd.exe 里双击运行（批处理语法只做人工审查 + 路径引用检查）；
   真实 Windows 桌面会话里原生 `qwindows` 插件画出来的窗口（CI 用 Qt 的离屏插件，
   因为 runner 没有可靠的桌面会话）；Windows 上的只读介质回退
   （只读/无写权限那套只在 Linux 成品上端到端测过）。
2. **只验证了 x86_64**。aarch64/arm64 需要另加平台描述（`PlatformSpec`），
   目前只有 `linux` 与 `windows` 两个 x86_64 目标。
3. **glibc 版本下限被抬高到 2.34**。按需求选了 `manylinux_2_34` 的 wheel，
   等价于要求 Ubuntu 22.04+ / Debian 12+ / RHEL 9+。要照顾更老的发行版，
   需要给 `fetch_runtime.py` 加一个 `--manylinux 2_17` 之类的开关
   （`MANYLINUX_PREFERENCE` 里已经有候选顺序），代价是有些项目没有 2_17 的 wheel。
4. **Linux 的 GUI 验证是在 Xvfb（真实 X11 + xcb 插件）上做的**，不是在真实桌面
   会话里。做法：把成品 zip 解压出来，用
   `env -i HOME=... PATH=/usr/bin:/bin TMPDIR=/tmp DISPLAY=:99 ./run.sh` 启动，
   `xwininfo` 能看到标题为「韧传 EverSend」的 1080×760 窗口，
   用成品自带的 PySide6 抓屏也正常（界面上还列出了局域网里另一台实例，
   说明广播发现也是通的）。**Wayland 会话没有实测**（`libqwayland*.so` 和
   `wayland-*` 插件都完整保留在包里）。
5. **PyPI 的项目级 JSON 接口很慢**（`cryptography` 实测 197 s，`cffi` >3 分钟）。
   工具默认会等（上限 420 s，结果永久缓存）；急的话用 `--pin cffi=2.0.0` 之类的
   版本钉住走「版本级」接口（1~2 s）。
6. **github.com 在部分网络下完全连不通**（实测：`api.github.com` 18 s 有响应，
   而 `github.com:443` TCP 连接 95 s 超时、release 资源 0 字节）。
   工具内置镜像回退（NJU / USTC / gh-proxy / ghproxy），`--source mirror` 直接走镜像；
   sha256 始终来自 GitHub API，所以镜像只当传输通道，不会降低校验强度。
7. **并发构建**：两个构建进程写同一个 `--out` 会互相覆盖。zip 现在是先写
   `.part` 再原子改名，所以不会再出现「半截 zip」，但仍是「后写完的赢」。
   要并行构建请给不同的 `--out`。
8. **`verify_green.py` 需要约 1.5 GB 临时空间**（要把 372 MiB 的树解压 + 复制一份），
   默认放 `TMPDIR`；`/tmp` 是 tmpfs 的机器上建议用 `--work-dir` 指到大盘上。
9. **应用自身的 CLI 输出没有 `flush=True`**：启动器用 `PYTHONUNBUFFERED=1` 兜住了，
   但如果将来有人绕过启动器直接跑 `python -m eversend --cli | tee`，
   启动信息会卡在缓冲区里。建议应用侧在 `cli.py` 的启动 print 上加 `flush=True`。

---

## 10. 故障排查

| 现象 | 原因 / 处理 |
| --- | --- |
| `cannot resolve release ... via the GitHub API` | `api.github.com` 不通。先手工把 tarball 放进 `tools/.cache/downloads/`，再 `--offline` |
| 下载卡在 0 字节 | `github.com` 被墙。用 `--source mirror` |
| `no wheel of X matches linux/windows` | 该项目没有对应平台 tag 的 wheel（看报错里列出的已发布文件名） |
| `RECORD mismatch` | 下载到的 wheel 与官方哈希不符（理论上不可能，因为先校验过 sha256）；重下 `--force` |
| `--strip` 后构建报「broke the Qt import」 | 裁剪名单过激，去掉 `--strip` 重建；并把删掉的东西记到 §7 的表里 |
| 构建自检 `SELFTEST FAILED: bind tcp/52117` | 端口被别的程序占用；现在这种情况只报 warning。真失败会明确打印 `NOT bindable` |
| 成品启动报 `Could not load the Qt platform plugin "xcb"` | 系统缺少 Qt 需要的 X 库（`libxkbcommon`、`libxcb-*`、`libGL` 等）。用 `ldd` 看缺哪个；这是宿主机的依赖，不在包内 |
| 只读 U 盘上提示数据改存临时目录 | 正常行为，见 §6.4 |

---

## 11. 文件清单

| 文件 | 作用 |
| --- | --- |
| `tools/fetch_runtime.py` | 下载内置解释器 + 全部 wheel，校验、解包到构建缓存 |
| `tools/build_green.py` | 组装成品树、生成启动器与 README、编译字节码、可选裁剪、打 zip |
| `tools/green_main.py` | 成品里的入口脚本（默认 GUI / `--cli` / `--selftest` / `--selftest-transfer` / `--print-paths`） |
| `tools/verify_green.py` | 端到端验证：外来机器模拟、移动后重跑、只读介质、`$HOME` 零污染 |
| `tools/.cache/` | 下载缓存与暂存目录（可删，已 gitignore） |
| `tools/.build/` | 构建输出（`EverSend/` 目录树 + `EverSend-<版本>-<平台>.zip`，已 gitignore） |
