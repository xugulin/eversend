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

    buildTypes {
        release {
            isMinifyEnabled = false
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
