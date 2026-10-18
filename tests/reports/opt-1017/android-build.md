# 安卓 APK 可复现构建（本地工具链落地）· opt-1017

> 负责人：android-device · 日期：2026-10-17 · 仓库：`/home/xgl/我的项目/自制软件/韧传/eversend`
> 设备：MI 9 (cepheus) `2430d3c`，crDroid 12.12 / Android 16 (API 36) / arm64-v8a。
> 结论：**本机已能独立产出 arm64 debug APK**，首次全量构建 11m19s（含依赖下载），产物 71.7 MB。

## 1. 改前状态（为什么需要这一节）

本机原本**没有** JDK / Android SDK / Gradle：

```
$ java -version   → bash: java: 未找到命令
$ which java javac gradle aapt aapt2 → 全空（只有 /usr/bin/adb，是 distro 的 platform-tools）
$ ls ~/android-dev → 不存在
```

`eversend/android/local.properties` 原本就写着 `sdk.dir=/home/xgl/android-dev/sdk`（提交进 git 的
开发机路径），所以本次安装**刻意**按这个路径落盘，构建脚本零改动即可工作。

## 2. 安装内容与来源（可复现）

全部装在 `~/android-dev/`（**不在 git 仓库内**；`.gitignore` 无关，仓库零变更）：

| 组件 | 版本 | 下载源 | 大小 |
|---|---|---|---|
| Temurin JDK17 | 17.0.20.1+1 | 清华 Adoptium 镜像 `https://mirrors.tuna.tsinghua.edu.cn/Adoptium/17/jdk/x64/linux/OpenJDK17U-jdk_x64_linux_hotspot_17.0.20.1_1.tar.gz` | 185 MB |
| Android cmdline-tools | 11076708 (latest) | `https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip` | 147 MB |
| Gradle | 8.7-bin | 腾讯镜像 `https://mirrors.cloud.tencent.com/gradle/gradle-8.7-bin.zip` | 128 MB |
| platforms;android-34 | 34 | sdkmanager（dl.google.com） | — |
| build-tools;34.0.0 | 34.0.0 | sdkmanager | — |
| platform-tools | 35.x | sdkmanager | — |

版本依据 `.github/workflows/android-app.yml`：`temurin 17` + `gradle 8.7` + `compileSdk 34 / minSdk 24`
（`android/build.gradle.kts` 里 AGP 8.5.2 → 必须 Gradle ≥8.7 + JDK17）。

最终布局（445 MB SDK + 318 MB JDK + 143 MB Gradle）：

```
~/android-dev/
├── jdk17/                 # Temurin 17.0.20.1
├── gradle-8.7/            # gradle-8.7/bin/gradle
├── sdk/                   # ANDROID_HOME
│   ├── cmdline-tools/latest/bin/sdkmanager
│   ├── platforms/android-34/android.jar
│   ├── build-tools/34.0.0/{aapt,aapt2,d8,…}
│   └── platform-tools/adb
├── dl/                    # 三个安装包缓存（可删，复现用）
├── init.gradle            # 依赖仓库加速（阿里云优先 + 官方兜底）
├── install.sh             # 一键安装（本次实际用的脚本）
├── build.sh               # 一键构建
└── logs/{install.log,build.log}
```

### 2.1 依赖仓库加速（国内网络必需）

`~/android-dev/init.gradle`：把 `pluginManagement` 与 `dependencyResolutionManagement` 的仓库
**先清空再加**：阿里云 `gradle-plugin`/`google`/`central`/`public` 在前，`google()`/`mavenCentral()`
兜底。构建时 `gradle -I ~/android-dev/init.gradle`。没有它也能构建，但 AGP/Kotlin/Compose
约 300 MB 依赖走直连很慢且易断。

```groovy
settingsEvaluated { settings ->
    settings.pluginManagement.repositories {
        clear()
        maven { url = uri('https://maven.aliyun.com/repository/gradle-plugin') }
        maven { url = uri('https://maven.aliyun.com/repository/google') }
        maven { url = uri('https://maven.aliyun.com/repository/central') }
        gradlePluginPortal(); google(); mavenCentral()
    }
    settings.dependencyResolutionManagement.repositories {
        clear()
        maven { url = uri('https://maven.aliyun.com/repository/google') }
        maven { url = uri('https://maven.aliyun.com/repository/central') }
        maven { url = uri('https://maven.aliyun.com/repository/public') }
        google(); mavenCentral()
    }
}
```

### 2.2 一键复现步骤（从零到 APK）

```bash
# 1) 装工具链（约 16 分钟；本机实测 954s，其中下载 25s、SDK packages 约 15.5 分钟）
bash ~/android-dev/install.sh

# 2) 构建（首次 11m19s，含依赖下载；之后增量 1–3 分钟）
bash ~/android-dev/build.sh assembleDebug

# 3) 产物（ABI 分包，android/app/build.gradle.kts 的 splits.abi）
ls -l android/app/build/outputs/apk/debug/
#   app-arm64-v8a-debug.apk   ← 手机用（真机 MI9）
#   app-x86_64-debug.apk      ← CI 模拟器用
```

`install.sh` 做四件事：① 三路 `curl` 并行下载 → ② `tar`/`unzip` 解压 JDK/Gradle →
③ cmdline-tools 摆到 `sdk/cmdline-tools/latest` → ④ `yes | sdkmanager --licenses` 接受许可后
`sdkmanager --install platform-tools platforms;android-34 build-tools;34.0.0`。
脚本会先轮询等下载文件完整（`unzip -t`/`tar -tzf` 校验通过才解压），可重入。

`build.sh` 设好 `JAVA_HOME/ANDROID_HOME/ANDROID_SDK_ROOT/PATH`，然后：

```bash
cd eversend/android
gradle -I ~/android-dev/init.gradle --no-daemon --console=plain assembleDebug
```

## 3. 构建结果与产物验证

### 3.1 首次构建（基线代码 e18c079）

```
=========== build start 2026-10-17T22:41:04+08:00 ===========
java: openjdk version "17.0.20.1"
gradle: Gradle 8.7
sdk: android-34  build-tools: 34.0.0
…
BUILD SUCCESSFUL in 11m 19s
39 actionable tasks: 39 executed
=========== build end 2026-10-17T22:52:29+08:00 rc=0 elapsed=684s ===========
app/build/outputs/apk/debug/app-arm64-v8a-debug.apk   71745227 bytes
app/build/outputs/apk/debug/app-x86_64-debug.apk      79674783 bytes
```

产物校验（`aapt` / `unzip`，见 §4 命令）：
- 包名 `cn.eversend.app`，versionName `1.0.0`，minSdk 24 / targetSdk 34 / compileSdk 34；
- `lib/arm64-v8a/` 下有 `libvlc.so`、`libc++_shared.so` 等（内置 VLC 解码器，包大的原因）；
- arm64 包 **不含** x86_64 的 so（ABI 分包生效）。

### 3.2 第二版构建（含 P0/P1 修复）

见本文 §6「增量构建耗时」与 `android-audit.md` 的修复清单。第二版 APK 的路径与 sha256 以
Lead 收到的那条消息为准（同路径、同 ABI 分包）。

## 4. 验证命令（可复现）

```bash
export JAVA_HOME=$HOME/android-dev/jdk17 ANDROID_HOME=$HOME/android-dev/sdk
APK=eversend/android/app/build/outputs/apk/debug/app-arm64-v8a-debug.apk
# 包名/版本/SDK/ABI
$ANDROID_HOME/build-tools/34.0.0/aapt dump badging "$APK" | grep -E "package:|sdkVersion|targetSdkVersion|native-code"
# ABIs 与 so
unzip -l "$APK" | grep -E "lib/arm64-v8a/(libvlc|libc\+\+_shared)\.so" | head
# sha256
sha256sum "$APK"
# 装机（Lead 授权的窗口内）
adb -s 2430d3c install -r "$APK"
```

## 5. 坑与注意（本机实测）

1. **`sdkmanager` 下载慢且不均匀**：cmdline-tools 147 MB/25s（~6 MB/s），但 `platforms;android-34`
   的 CDN 节点只有 ~140 KB/s，SDK packages 总耗时约 15.5 分钟。**不要中途 kill**，它是分文件下载后
   统一解压（`.temp/PackageOperation01/`），断了要重来。判断"是否在动"：`find ~/android-dev/sdk/.temp -name '*.zip' -exec ls -la {} \;` 看大小增长，或 `jstack <pid>` 看是否停在
   `LegacyDownloader.downloadFully`（那就是在正常下载，不是卡死）。
2. **`yes | sdkmanager --licenses` 会报 `断开的管道`**：这是 `yes` 被 SIGPIPE，正常现象；
   退出码 0 即可。`--install` 也必须先跑过 licenses，否则会**卡在交互式 y/N 等待**（stdin 是
   `/dev/null` 时表现为"假死"）。
3. **仓库里没有 gradle wrapper**（`android/` 下无 `gradlew`/`gradle/wrapper`），必须用系统
   Gradle 8.7；AGP 8.5.2 不接受更低版本。
4. **脚本要 chmod +x**：本次 `build.sh` 写盘后没有执行位，直接跑会 `Permission denied`（exit 126）
   而异常信息容易淹没——用 `bash ~/android-dev/build.sh` 或先 `chmod +x`。
5. **`--no-daemon` 下 gradle 仍会 fork 一个 single-use daemon**（日志里有提示，正常），
   `-Xmx2048m` 来自 `android/gradle.properties`，本机 8.9 负载下构建未被 OOM。
6. **首次构建必须联网**（依赖 ~300 MB），之后 `~/.gradle/caches` 有缓存，增量构建只重编译改动
   的 Kotlin 文件（增量第二版实测见 §6）。
7. **不要用 `gradle clean`** 做日常增量：clean 会丢 `mergeDebugJniLibFolders` 缓存，重新打包 70 MB
   so 很慢（~1 分钟）；改代码直接 `assembleDebug`。
8. 构建输出在 `android/app/build/`（已被仓库 `.gitignore` 覆盖），不要提交；`local.properties`
   是本地路径文件，同样不提交（仓库里这份是给本机用）。
9. 手机装 APK 前先确认**没有旧包在跑真机用例**（`adb shell dumpsys activity activities | grep eversend`），
   `adb install -r` 会杀掉当前进程，正在传输的文件会中断。

## 6. 增量构建耗时（真机实测）

| 构建 | 触发 | 结果 | 耗时 | 说明 |
|---|---|---|---|---|
| #1 全量（基线 e18c079） | 22:41:04 | ✅ SUCCESS | **684s (11m19s)** | 含 AGP/Kotlin/Compose/libvlc 依赖下载（阿里云镜像）+ 全量编译打包 |
| #2a v2（P0/A+B、P1-C/D 修复） | 22:59:47 | ❌ 编译失败 | 460s | 唯一错误 `MainActivity.kt:7244 Only safe (?.)… on nullable JSONObject`（新代码 `caps.optLong`），修一行 |
| #2b v2 | 23:10 左右 | ✅ SUCCESS | **447s (7m26s)** | `5 executed / 34 up-to-date`；此时机器负载 8.9，Kotlin 编译本身占大头（--no-daemon 每次冷启动 Kotlin daemon） |
| #3 v3（PG-1/2/3/5/6 + 扫描计数/兜底冷却） | 23:3x | 见日志 | — | 增量 |

> 经验：**改 Kotlin 代码的增量构建约 5–8 分钟（负载高时），不是 1–2 分钟**；只有纯资源/manifest
> 改动才会 1 分钟内完成（manifest 改动会触发 `processDebugManifest` 但 Kotlin 不动）。
> 计划构建时长时按"每次代码改动 ≈ 7 分钟"算。

### 6.1 v2 APK 校验与安装（真机实测）

```
path   : eversend/android/app/build/outputs/apk/debug/app-arm64-v8a-debug.apk
size   : 72,251,719 B（v1 基线 71,745,227 B；+506 KB 来自修复代码）
sha256 : 9cae120c95fb4c16d798bed8290ff035efe59d6fa5371b3285c971238a265884
aapt   : package cn.eversend.app, versionCode 1, versionName 1.0.0,
         minSdk 24 / targetSdk 34 / compileSdk 34, native-code 'arm64-v8a'
unzip  : lib/arm64-v8a/ 4 个 so（libvlc.so 45,252,696 B 等）；无 x86_64（ABI 分包生效）
```

**安装坑（重要）**：旧 APK 由 CI/另一台机器用**不同的 debug keystore** 签名，本机构建的 APK
`adb install -r` 报：

```
INSTALL_FAILED_UPDATE_INCOMPATIBLE: Existing package cn.eversend.app signatures do not match newer version
```

唯一解是 `adb uninstall cn.eversend.app` 后重装 —— **会清掉 App 本地数据**（deviceId/host/
keepAlive 偏好），电脑端会把它当一台新设备。之后用本机 `~/.android/debug.keystore` 构建的
后续版本（v3、v4…）签名一致，可以正常 `install -r` 覆盖，**不需要再清数据**。
验证签名一致性：`apksigner verify --print-certs <apk>` 或直接看 install 是否报上面的错。
