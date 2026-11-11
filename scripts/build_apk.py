"""手工构建主人端 APK（不依赖 Gradle / 不联网）。

为什么不用 Gradle（2026-11-11 用户环境实测）：Gradle 首次构建要从 dl.google.com 拉
AGP/Kotlin/Gradle 发行版，本机策略代理对 Google 一律 403；而 `aapt2 + d8 + apksigner`
只用本地 SDK 文件，断网也能出包。

流程（全部标准工具，无自写打包逻辑）：
    aapt2 link  → 带资源的空 APK（含二进制 manifest）
    javac       → class 文件（classpath = android.jar）
    d8          → classes.dex
    zip 追加    → classes.dex 塞进 APK
    zipalign    → 4 字节对齐（targetSdk>=30 必须）
    apksigner   → v1+v2 签名（debug keystore，首次自动生成）

用法：
    py -3.12 scripts/build_apk.py
    py -3.12 scripts/build_apk.py --sdk D:\\Android\\Sdk --java-home "D:\\Android\\Android Studio\\jbr"
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANDROID_DIR = os.path.join(ROOT, "android")
BUILD_DIR = os.path.join(ANDROID_DIR, "build")

APP_PACKAGE = "com.sentinel.master"
APP_LABEL = "sentinel-master"
MIN_SDK = 24
TARGET_SDK = 36
VERSION_CODE = "1"
VERSION_NAME = "0.1.0"


def find_default_sdk():
    for candidate in (os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
                      r"D:\Android\Sdk",
                      os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk")):
        if candidate and os.path.isdir(candidate):
            return candidate
    raise SystemExit("找不到 Android SDK：用 --sdk 指定（目录里应有 platforms/ build-tools/）")


def find_default_java_home():
    for candidate in (os.environ.get("JAVA_HOME"),
                      r"D:\Android\Android Studio\jbr",
                      r"C:\Program Files\Android\Android Studio\jbr"):
        if candidate and os.path.isfile(os.path.join(candidate, "bin", "java.exe")):
            return candidate
    raise SystemExit("找不到 JDK：用 --java-home 指定（Android Studio 自带 jbr 即可）")


def run(command, env=None):
    print(">", " ".join(os.path.basename(part) if i == 0 else part
                        for i, part in enumerate(command)), flush=True)
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stdout.write(result.stdout or "")
        sys.stderr.write(result.stderr or "")
        raise SystemExit("命令失败（exit %d）：%s" % (result.returncode, command[0]))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="手工构建主人端 APK")
    parser.add_argument("--sdk", default=None)
    parser.add_argument("--java-home", default=None)
    parser.add_argument("--build-tools", default="36.0.0")
    parser.add_argument("--platform", default="android-36")
    parser.add_argument("--out", default=os.path.join(BUILD_DIR, APP_LABEL + "-debug.apk"))
    args = parser.parse_args(argv)

    sdk = os.path.abspath(args.sdk or find_default_sdk())
    java_home = os.path.abspath(args.java_home or find_default_java_home())
    build_tools = os.path.join(sdk, "build-tools", args.build_tools)
    android_jar = os.path.join(sdk, "platforms", args.platform, "android.jar")
    aapt2 = os.path.join(build_tools, "aapt2.exe" if os.name == "nt" else "aapt2")
    d8 = os.path.join(build_tools, "d8.bat" if os.name == "nt" else "d8")
    zipalign = os.path.join(build_tools, "zipalign.exe" if os.name == "nt" else "zipalign")
    apksigner = os.path.join(build_tools, "apksigner.bat" if os.name == "nt" else "apksigner")
    javac = os.path.join(java_home, "bin", "javac.exe" if os.name == "nt" else "javac")
    jar_tool = os.path.join(java_home, "bin", "jar.exe" if os.name == "nt" else "jar")
    keytool = os.path.join(java_home, "bin", "keytool.exe" if os.name == "nt" else "keytool")

    for path in (aapt2, d8, zipalign, apksigner, javac, keytool, android_jar):
        if not os.path.exists(path):
            raise SystemExit("缺少工具：%s" % path)

    env = dict(os.environ)
    env["JAVA_HOME"] = java_home
    env["PATH"] = os.path.join(java_home, "bin") + os.pathsep + env.get("PATH", "")

    work = os.path.join(BUILD_DIR, "work")
    if os.path.isdir(work):
        shutil.rmtree(work)
    os.makedirs(work, exist_ok=True)
    classes_dir = os.path.join(work, "classes")
    dex_dir = os.path.join(work, "dex")
    os.makedirs(classes_dir)
    os.makedirs(dex_dir)

    unsigned = os.path.join(work, "app-unsigned.apk")
    aligned = os.path.join(work, "app-aligned.apk")
    keystore = os.path.join(BUILD_DIR, "debug.keystore")

    # 1) 资源链接：没有 res 目录也能出包，manifest 里的 label 是字面量
    run([aapt2, "link", "-o", unsigned,
         "-I", android_jar,
         "--manifest", os.path.join(ANDROID_DIR, "AndroidManifest.xml"),
         "--min-sdk-version", str(MIN_SDK),
         "--target-sdk-version", str(TARGET_SDK),
         "--version-code", VERSION_CODE,
         "--version-name", VERSION_NAME])

    # 2) 编译 Java（classpath 用 android.jar，不用 --release，避免 JDK 自带的平台类抢先）
    java_sources = []
    for base, _dirs, files in os.walk(os.path.join(ANDROID_DIR, "src")):
        java_sources.extend(os.path.join(base, name) for name in files if name.endswith(".java"))
    if not java_sources:
        raise SystemExit("android/src 下没有 .java")
    run([javac, "-source", "11", "-target", "11", "-encoding", "UTF-8",
         "-classpath", android_jar, "-d", classes_dir] + java_sources, env=env)

    # 3) 打成 jar 再交给 d8（这个版本的 d8 不直接吃目录）
    classes_jar = os.path.join(work, "classes.jar")
    run([jar_tool, "cf", classes_jar, "-C", classes_dir, "."], env=env)
    run([d8, "--release", "--lib", android_jar, "--min-api", str(MIN_SDK),
         "--output", dex_dir, classes_jar], env=env)

    # 4) classes.dex 追加进 APK（APK 就是 zip）
    dex_path = os.path.join(dex_dir, "classes.dex")
    if not os.path.exists(dex_path):
        raise SystemExit("d8 没有产出 classes.dex")
    with zipfile.ZipFile(unsigned, "a", zipfile.ZIP_DEFLATED) as zf:
        zf.write(dex_path, "classes.dex")

    # 5) 对齐 + 签名
    run([zipalign, "-f", "-p", "4", unsigned, aligned])
    if not os.path.exists(keystore):
        run([keytool, "-genkeypair", "-keystore", keystore, "-alias", "androiddebugkey",
             "-storepass", "android", "-keypass", "android", "-keyalg", "RSA",
             "-keysize", "2048", "-validity", "10000",
             "-dname", "CN=Android Debug,O=Android,C=US"], env=env)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    run([apksigner, "sign", "--ks", keystore, "--ks-pass", "pass:android",
         "--key-pass", "pass:android", "--out", args.out, aligned], env=env)
    run([apksigner, "verify", "--verbose", args.out], env=env)

    size = os.path.getsize(args.out)
    print("\n[build_apk] OK  %s  (%.1f KB)" % (args.out, size / 1024.0))
    print("[build_apk] 安装：adb install -r \"%s\"" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())