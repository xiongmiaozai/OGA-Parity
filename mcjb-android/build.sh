#!/usr/bin/env bash
# MC 材质转换器 Android APK 构建脚本
# 纯手工构建链：aapt2 -> javac -> d8 -> zip -> zipalign -> apksigner
# 不依赖 Gradle / 网络，使用 /opt/android-sdk 内已安装的组件。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
SDK=/opt/android-sdk
BT="$SDK/build-tools/34.0.0"
PLATFORM="$SDK/platforms/android-34/android.jar"
AAPT2="$BT/aapt2"
D8="$BT/d8"
ZIPALIGN="$BT/zipalign"
APKSIGNER="$BT/apksigner"

B="$ROOT/build"
OUT="$ROOT/MC材质转换器-v1.0.apk"
PKG=com.mcjb2vv.app

echo "==> [0/8] 清理"
rm -rf "$B"
mkdir -p "$B/gen" "$B/classes" "$B/dex" "$B/flat"

echo "==> [1/8] aapt2 compile 资源"
"$AAPT2" compile --dir "$ROOT/res" -o "$B/flat/res.zip"

echo "==> [2/8] aapt2 link 生成 R.java + 基础 APK"
"$AAPT2" link \
  -o "$B/base.apk" \
  -I "$PLATFORM" \
  --manifest "$ROOT/AndroidManifest.xml" \
  -R "$B/flat/res.zip" \
  --java "$B/gen" \
  --min-sdk-version 24 \
  --target-sdk-version 34 \
  --version-code 1 \
  --version-name 1.0.0 \
  --auto-add-overlay

find "$B/gen" -name '*.java'

echo "==> [3/8] javac 编译"
find "$ROOT/src" "$B/gen" -name '*.java' > "$B/sources.txt"
javac -source 8 -target 8 \
  -encoding UTF-8 \
  -bootclasspath "$PLATFORM" \
  -classpath "$PLATFORM" \
  -d "$B/classes" \
  @"$B/sources.txt" 2> "$B/javac.log" || { cat "$B/javac.log"; exit 1; }
cat "$B/javac.log"

echo "==> [4/8] d8 生成 classes.dex"
find "$B/classes" -name '*.class' > "$B/classes.txt"
"$D8" --lib "$PLATFORM" --min-api 24 --output "$B/dex" @"$B/classes.txt"
ls -lh "$B/dex/classes.dex"

echo "==> [5/8] 打入 dex"
cp "$B/base.apk" "$B/unsigned.apk"
(cd "$B/dex" && zip -q "$B/unsigned.apk" classes.dex)

echo "==> [6/8] zipalign 对齐"
"$ZIPALIGN" -f -p 4 "$B/unsigned.apk" "$B/aligned.apk"

echo "==> [7/8] 生成签名密钥（首次）"
KS="$ROOT/mcjb-release.keystore"
if [ ! -f "$KS" ]; then
  keytool -genkeypair -v \
    -keystore "$KS" -alias mcjb -keyalg RSA -keysize 2048 -validity 10950 \
    -storepass mcjb2026 -keypass mcjb2026 \
    -dname "CN=MCJB2VV, OU=Tools, O=MCJB2VV, L=CN, S=CN, C=CN" > "$B/keytool.log" 2>&1
  cat "$B/keytool.log"
fi

echo "==> [8/8] apksigner 签名（v1+v2+v3）"
"$APKSIGNER" sign \
  --ks "$KS" --ks-key-alias mcjb \
  --ks-pass pass:mcjb2026 --key-pass pass:mcjb2026 \
  --v1-signing-enabled true --v2-signing-enabled true --v3-signing-enabled true \
  --out "$OUT" "$B/aligned.apk"

echo "==> 校验"
"$APKSIGNER" verify --verbose --print-certs "$OUT"
echo ""
echo "==> 产物"
ls -lh "$OUT"
