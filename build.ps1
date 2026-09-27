# ============================================================
# fnMonitor 构建脚本 (Windows / PowerShell)
# 用法: 右键"使用 PowerShell 运行"，或在该目录执行 .\build.ps1
# 前提: 已安装 Python 3
#       fnpack：优先使用本目录 fnpack.exe，其次取 PATH 中的 fnpack
#       官方文档 https://developer.fnnas.com/docs/cli/fnpack/
# 产物: fnmonitor-<版本>-x86.fpk 与 fnmonitor-<版本>-arm.fpk
# ============================================================
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

Write-Host "[1/4] 生成应用图标..." -ForegroundColor Cyan
python make_icon.py

Write-Host "[2/4] 检查 fnpack..." -ForegroundColor Cyan
if (Test-Path ".\fnpack.exe") {
    $fnpack = ".\fnpack.exe"
} else {
    $cmd = Get-Command fnpack -ErrorAction SilentlyContinue
    if ($cmd) { $fnpack = $cmd.Source } else { $fnpack = $null }
}
if (-not $fnpack) {
    Write-Host "未找到 fnpack 命令。" -ForegroundColor Yellow
    Write-Host "  1) 下载 fnpack Windows 版: https://developer.fnnas.com/docs/cli/fnpack/" -ForegroundColor Yellow
    Write-Host "  2) 将 fnpack.exe 放到本目录，或加入 PATH 后重试" -ForegroundColor Yellow
    exit 1
}

Write-Host "[3/4] 读取 manifest 版本号..." -ForegroundColor Cyan
$manifestPath = Join-Path $root "manifest"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$orig = [System.IO.File]::ReadAllText($manifestPath, $utf8NoBom)
$version = ([regex]::Match($orig, '(?m)^version=(.+)$')).Groups[1].Value.Trim()
if (-not $version) {
    Write-Host "无法从 manifest 解析 version" -ForegroundColor Red
    exit 1
}
Write-Host "版本号: $version" -ForegroundColor Green

try {
    Write-Host "[4/4] 分别打包 x86 / arm ..." -ForegroundColor Cyan
    foreach ($plat in @("x86", "arm")) {
        $swapped = [regex]::Replace($orig, '(?m)^platform=.*$', "platform=$plat")
        [System.IO.File]::WriteAllText($manifestPath, $swapped, $utf8NoBom)
        & $fnpack build
        if ($LASTEXITCODE -ne 0) { throw "fnpack build ($plat) 失败" }
        $out = "fnmonitor-$version-$plat.fpk"
        Move-Item -Force "fnmonitor.fpk" $out
        Write-Host "  -> $out" -ForegroundColor Green
    }
}
finally {
    # 无论成功与否，恢复源 manifest 的原始 platform
    [System.IO.File]::WriteAllText($manifestPath, $orig, $utf8NoBom)
}

Write-Host ""
Write-Host "打包完成！" -ForegroundColor Green
Write-Host "  fnmonitor-$version-x86.fpk （x86 机型）" -ForegroundColor Green
Write-Host "  fnmonitor-$version-arm.fpk （arm64 机型）" -ForegroundColor Green
Write-Host "在飞牛 OS 应用中心 -> 左下角"手动安装" -> 按架构选择 fpk 安装。" -ForegroundColor Green
