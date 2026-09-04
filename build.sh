#!/bin/bash
# ============================================================
# fnMonitor 构建脚本 (Linux / 飞牛 OS)
# 用法: bash build.sh
# 前提: 飞牛 OS 已预置 fnpack（可直接使用），需要 python3
# ============================================================
set -e
cd "$(dirname "$0")"

echo "[1/3] 生成应用图标..."
python3 make_icon.py

echo "[2/3] 检查 fnpack..."
if ! command -v fnpack >/dev/null 2>&1; then
    echo "错误：未找到 fnpack 命令。"
    echo "  飞牛 OS 预置了 fnpack，若提示找不到，请确认 PATH 包含 /usr/local/bin"
    echo "  或在本地下载 fnpack-1.2.1-linux-amd64 并放入 /usr/local/bin"
    exit 1
fi

echo "[3/3] 打包 fpk ..."
fnpack build
echo ""
echo "打包完成！"
echo "将生成的 .fpk 文件拷贝到飞牛 OS，在 应用中心 -> 手动安装 中安装。"
