// 韧传 EverSend · Android
//
// 为什么是原生 App，而不是继续用手机浏览器页面
// --------------------------------------------
// 网页版受浏览器约束：熄屏、切到后台、系统省电都会把页面冻结，连接就断了；
// 麦克风还要求 HTTPS。这些都是浏览器的硬限制，改不了。原生 App 用前台服务持有
// 长连接，熄屏、切后台、锁屏都能继续收消息 —— 这是这次开发它的唯一理由。
//
// 为什么 Emoji 用 Compose 的 Text 而不是自己做渲染
// ------------------------------------------------
// Compose 的文本走系统字体栈，安卓系统字体本身就带彩色 emoji（Noto Color Emoji），
// 所以 emoji 的显示质量等于系统相册、微信的水平；输入用系统键盘的 emoji 面板，
// 再配一个内置面板（同样是 Unicode 码位，由系统字体渲染）。任何"自带字体/自带图形"
// 的方案（Kivy、图片表情包）都做不到这一点。
plugins {
    id("com.android.application") version "8.5.2"
    id("org.jetbrains.kotlin.android") version "1.9.24"
}

android {
    namespace = "cn.eversend.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "cn.eversend.app"
        minSdk = 24
        targetSdk = 34
        versionCode = 1
        versionName = "1.0.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        // The desktop the app should talk to; overridden at runtime (discovery,
        // typed address, or an intent extra in the CI test).
        buildConfigField("String", "DEFAULT_HOST", "\"\"")
    }

    // 按 ABI 分开打包：一个"全能包"要塞进 libvlc.so 的两份（arm64 43 MB +
    // x86_64 51 MB），120 MB 起步；分开之后手机拿到的是 ~60 MB 的 arm64 包，
    // CI 的模拟器装 x86_64 那份，谁也不背别人的重量。
    // 只保留两种 ABI：arm64（2016 年后的手机都是它）和 x86_64（CI 的模拟器）。
    splits {
        abi {
            isEnable = true
            reset()
            include("arm64-v8a", "x86_64")
            isUniversalApk = false
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            // 用 debug 密钥签名：这是自用/测试分发，正式上架要换自己的密钥。
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures {
        compose = true
        buildConfig = true
    }
    composeOptions { kotlinCompilerExtensionVersion = "1.5.14" }
    packaging {
        resources.excludes += "/META-INF/{AL2.0,LGPL2.1}"
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-compose:1.9.2")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.6")
    implementation("androidx.lifecycle:lifecycle-service:2.8.6")
    implementation("androidx.compose.ui:ui:1.7.2")
    implementation("androidx.compose.material3:material3:1.3.0")
    implementation("androidx.compose.material:material-icons-extended:1.7.2")
    implementation("androidx.compose.ui:ui-tooling-preview:1.7.2")
    implementation("androidx.emoji2:emoji2:1.5.0")
    implementation("androidx.emoji2:emoji2-views-helper:1.5.0")
    // 自带解码器的播放内核（libVLC 内含 FFmpeg）。用户明确要求"内置播放能力、
    // 不要依赖系统解码器"：安卓并不保证 MKV/HEVC 能解，而手机录音和网页录音
    // 又是 webm/opus 与 m4a 混着来。libVLC 一个依赖就把这些全覆盖了。
    //
    // 版本**不能**跟着 Gradle 的 latest 走：libvlc-all 的 <release> 是 4.0.0-eap，
    // 它要求 minCompileSdk=36，而本项目是 compileSdk 34（3.7.3 起就要 37）。
    // 3.7.2 是仍然兼容的最后一个版本。
    implementation("org.videolan.android:libvlc-all:3.7.2")

    debugImplementation("androidx.compose.ui:ui-tooling:1.7.2")

    androidTestImplementation("androidx.test.ext:junit:1.2.1")
    androidTestImplementation("androidx.test:runner:1.6.2")
    androidTestImplementation("androidx.test:rules:1.6.1")
    androidTestImplementation("androidx.test:core-ktx:1.6.1")
    androidTestImplementation("androidx.test.espresso:espresso-core:3.6.1")
    androidTestImplementation("androidx.compose.ui:ui-test-junit4:1.7.2")
    debugImplementation("androidx.compose.ui:ui-test-manifest:1.7.2")
    androidTestImplementation("androidx.test.uiautomator:uiautomator:2.3.0")
}
