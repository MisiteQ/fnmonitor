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

Write-Host "[1/5] 从 ui-src 重建前端..." -ForegroundColor Cyan
# app/www/index.html 是 ui-src 的生成产物，直接改它会导致 CI 的
# 「index.html 与 ui-src 不一致」检查失败；打包前强制重建，避免产物漂移。
$sh = Get-Command sh -ErrorAction SilentlyContinue
$py3 = Get-Command python3 -ErrorAction SilentlyContinue
if (-not $py3 -and $sh) {
    $py = Get-Command python -ErrorAction SilentlyContinue
    if ($py -and $py.Source -notlike "*WindowsApps*") {
        # Windows 通常只有 python.exe，这里临时造一个 python3 供 build_ui.sh 调用
        $shim = Join-Path $env:TEMP "fnm-py3shim"
        New-Item -ItemType Directory -Force $shim | Out-Null
        Copy-Item $py.Source (Join-Path $shim "python3.exe") -Force
        $env:PYTHONHOME = Split-Path $py.Source -Parent
        $env:PATH = "$shim;" + $env:PATH
        $py3 = Get-Command python3 -ErrorAction SilentlyContinue
    }
}
if ($sh -and $py3) {
    & $sh.Source tools/build_ui.sh
    if ($LASTEXITCODE -ne 0) { throw "前端构建失败：请检查 ui-src/ 后重试" }
} else {
    Write-Host "  未找到 sh/python3，跳过前端重建；请确认 app/www/index.html 与 ui-src 一致" -ForegroundColor Yellow
}

Write-Host "[2/5] 生成应用图标..." -ForegroundColor Cyan
if ($py3) { & $py3.Source make_icon.py } else { python make_icon.py }
if ($LASTEXITCODE -ne 0) { throw "图标生成失败：请检查 Python 环境" }

# 清理 Python 字节码缓存：fnpack 会打包 app/ 下全部文件，__pycache__ 一旦残留（本地跑过服务/测试）就会混入 fpk
Get-ChildItem -Path $root -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "[3/5] 检查 fnpack..." -ForegroundColor Cyan
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

Write-Host "[4/5] 读取 manifest 版本号..." -ForegroundColor Cyan
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
    Write-Host "[5/5] 分别打包 x86 / arm ..." -ForegroundColor Cyan
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
