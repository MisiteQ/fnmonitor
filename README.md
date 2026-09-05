# 飞牛监控 (fnMonitor)

[![Release](https://img.shields.io/github/v/release/MisiteQ/fnmonitor)](https://github.com/MisiteQ/fnmonitor/releases)
[![Platform](https://img.shields.io/badge/platform-fnOS%20x86%20%7C%20ARM-blue)](https://github.com/MisiteQ/fnmonitor/releases)

飞牛 fnOS 系统监控应用（FPK 原生应用）：实时监控 **CPU / 内存 / 磁盘 / 网络 / 温度 / 功耗 / GPU**，支持 Docker 容器管理、端口占用、硬盘 SMART、历史趋势与七套主题。纯 Python 标准库 + 单文件前端，**零第三方依赖、完全离线可用**，数据仅保存在本机。

## ✨ 功能

- **🏠 总览仪表盘**：状态卡片（CPU / 内存 / 存储 / 网络 / 温度 / 负载 / 运行时间 / IP）、时间·农历与天气、资源详情（每核 CPU / 内存明细 / 磁盘 IO）、硬盘信息（逐盘 SMART + SATA 告警计数）、硬件与传感器（内存插槽 SPD / 温度墙 / 电压 / 风扇）、实时功耗（Intel RAPL）与显卡监控、八维历史趋势折线图、进程 TOP
- **🐳 Docker · 端口**：容器列表与资源占用、一键启动 / 重启 / 停止、端口占用（应用 / 容器 / 系统监听）、内置应用统计（相册 / 影视 / 音乐）
- **⚙️ 设置**：监听端口、采集间隔、历史保留天数、数据导出（CSV / JSON）、一键健康报告（HTML）
- **🎨 界面**：七套主题、现代 NAS 仪表盘风格（侧边栏可收起）、每个面板 / 小模块独立显示隐藏、拖动排序自动记忆、手机端底部导航自适应

## 📦 安装

1. 到 [Releases](https://github.com/MisiteQ/fnmonitor/releases) 下载对应架构的 `.fpk`（x86 / arm）
2. 飞牛 OS → **应用中心** → 左下角 **手动安装** → 选择 fpk 文件
3. 安装后桌面打开 **飞牛监控**，或直接访问 `http://<NAS_IP>:8777`

> 若「手动安装」入口被关闭，SSH 执行：`appcenter-cli manual-install enable`

## 🛠 从源码打包

```powershell
# Windows（需 Python 3 + fnpack，见 https://developer.fnnas.com/docs/cli/fnpack/）
.\build.ps1
```

```bash
# 飞牛 OS 上
bash build.sh
```

## 📋 近期更新

| 版本 | 内容 |
|---|---|
| v2.8.3 | 接入 FnDepot 外部应用源（仓库根目录 fnpack.json 索引，可在 FnDepot 客户端直接安装 / 升级）；无功能代码变更 |
| v2.8.2 | 修复 F11 全屏时浏览器标题栏 / 菜单栏不受控弹出（改走全屏 API + 顶栏全屏按钮）；手机底部虚拟按键适配：底部导航 / 悬浮按钮自动上移，不再被三键导航遮挡 |
| v2.8.1 | 修复「存储总使用」统计不准：按文件系统 ID 精确去重（存储池子卷 / 设备别名不再重复计算），排除网络挂载盘，口径与 fnOS 存储页对齐 |
| v2.8.0 | 修复资源趋势历史数据不显示（打开即加载全部历史）；侧边栏支持收起 / 展开；界面美化 |
| v2.7.1 | 修复小模块显示不全与平板竖屏适配 |

完整日志见 [Releases](https://github.com/MisiteQ/fnmonitor/releases)。

## 结构

```
app/server.py       后端：采集 + SQLite 历史 + HTTP API（零依赖）
app/www/index.html  前端：单文件面板（原生 JS + SVG 图表）
cmd/ wizard/ config/  飞牛 FPK 生命周期脚本与向导
```

---

MIT © [MisiteQ](https://github.com/MisiteQ)
