# ============================================================
# fnMonitor 构建脚本 (Windows / PowerShell)
# 用法: 右键"使用 PowerShell 运行"，或在该目录执行 .\build.ps1
# 前提: 已安装 Python 3，且 fnpack 已在 PATH 中
#       官方文档 https://developer.fnnas.com/docs/cli/fnpack/
# ============================================================
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

Write-Host "[1/3] 生成应用图标..." -ForegroundColor Cyan
python make_icon.py

Write-Host "[2/3] 检查 fnpack..." -ForegroundColor Cyan
$fnpack = Get-Command fnpack -ErrorAction SilentlyContinue
if (-not $fnpack) {
    Write-Host "未找到 fnpack 命令。" -ForegroundColor Yellow
    Write-Host "  1) 下载 fnpack Windows 版: https://developer.fnnas.com/docs/cli/fnpack/" -ForegroundColor Yellow
    Write-Host "  2) 将 fnpack 放到本目录后修改下方命令，或加入 PATH 后重试" -ForegroundColor Yellow
    exit 1
}

Write-Host "[3/3] 打包 fpk ..." -ForegroundColor Cyan
fnpack build
if ($LASTEXITCODE -ne 0) {
    Write-Host "打包失败，请检查上方报错信息。" -ForegroundColor Red
    exit 1
}

Write-Host "" -ForegroundColor Cyan
Write-Host "打包完成！" -ForegroundColor Green
Write-Host "在飞牛 OS 应用中心 -> 左下角"手动安装" -> 选择生成的 .fpk 文件即可安装。" -ForegroundColor Green
