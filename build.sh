#!/bin/bash
# ============================================================
# fnMonitor 构建脚本 (Linux / 飞牛 OS)
# 用法: bash build.sh
# 前提: 飞牛 OS 已预置 fnpack（可直接使用），需要 python3
# 产物: fnmonitor-<版本>-x86.fpk 与 fnmonitor-<版本>-arm.fpk
# ============================================================
set -e
cd "$(dirname "$0")"

echo "[1/4] 生成应用图标..."
python3 make_icon.py

echo "[2/4] 检查 fnpack..."
if [ -x ./fnpack ]; then
    FNPACK=./fnpack
elif command -v fnpack >/dev/null 2>&1; then
    FNPACK=fnpack
else
    echo "错误：未找到 fnpack 命令。"
    echo "  飞牛 OS 预置了 fnpack，若提示找不到，请确认 PATH 包含 /usr/local/bin"
    echo "  或在本地下载 fnpack-1.2.1-linux-amd64 并放入 /usr/local/bin"
    exit 1
fi

echo "[3/4] 读取 manifest 版本号..."
VERSION=$(sed -n 's/^version=//p' manifest | head -1 | tr -d '[:space:]')
if [ -z "$VERSION" ]; then
    echo "错误：无法从 manifest 解析 version"
    exit 1
fi
echo "版本号: $VERSION"

# 无论成功与否，结束后恢复源 manifest
trap 'cp -f "$TMP_MANIFEST" manifest 2>/dev/null || true' EXIT
TMP_MANIFEST=$(mktemp)
cp manifest "$TMP_MANIFEST"

echo "[4/4] 分别打包 x86 / arm ..."
for plat in x86 arm; do
    sed "s/^platform=.*/platform=$plat/" "$TMP_MANIFEST" > manifest
    "$FNPACK" build
    mv -f fnmonitor.fpk "fnmonitor-$VERSION-$plat.fpk"
    echo "  -> fnmonitor-$VERSION-$plat.fpk"
done

echo ""
echo "打包完成！"
echo "  fnmonitor-$VERSION-x86.fpk （x86 机型）"
echo "  fnmonitor-$VERSION-arm.fpk （arm64 机型）"
echo "将对应架构的 .fpk 拷贝到飞牛 OS，在 应用中心 -> 手动安装 中安装。"
