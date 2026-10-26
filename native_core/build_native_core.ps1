$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$src = Join-Path $PSScriptRoot "fukua_rpa_core.cpp"
$out = Join-Path $root "fukua_rpa_core.dll"
$buildDir = Join-Path $PSScriptRoot "build"
$vcvarsCandidates = @(
    "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat",
    "C:\Program Files\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat",
    # LEGACY: 仅历史兼容，新机器走 vswhere 自动发现。勿删除（向后兼容旧开发环境）。
    "E:\GongJv\Visual_Studio\VC\Auxiliary\Build\vcvars64.bat"
)

$vcvars = $vcvarsCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $vcvars) {
    $vswhereCandidates = @(
        "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe",
        "${env:ProgramFiles}\Microsoft Visual Studio\Installer\vswhere.exe"
    )
    $vswhere = $vswhereCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($vswhere) {
        $installPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
        if ($installPath) {
            $discovered = Join-Path $installPath "VC\Auxiliary\Build\vcvars64.bat"
            if (Test-Path $discovered) {
                $vcvars = $discovered
            }
        }
    }
}
if (-not $vcvars) {
    throw "Cannot find vcvars64.bat. Install Visual Studio Build Tools with C++ workload."
}

New-Item -ItemType Directory -Force -Path $buildDir | Out-Null

# Remove stale output DLL to avoid LNK1104 if a previous process holds it.
if (Test-Path -LiteralPath $out) {
    try {
        Remove-Item -LiteralPath $out -Force
    } catch {
        throw "Cannot remove $out — the file is locked by another process. Close any running fukuaRPA instance or test host, then retry."
    }
}

$obj = Join-Path $buildDir "fukua_rpa_core.obj"
$implib = Join-Path $buildDir "fukua_rpa_core.lib"
$dependencyReport = Join-Path $buildDir "fukua_rpa_core.dependencies.txt"
$headerReport = Join-Path $buildDir "fukua_rpa_core.headers.txt"

$cmd = "call `"$vcvars`" >nul && cl /nologo /O2 /MT /EHsc /std:c++17 /W4 " +
       "/D_WIN32_WINNT=0x0A00 /DWINVER=0x0A00 /LD `"$src`" /Fo`"$obj`" " +
       "/link /DLL /OUT:`"$out`" /IMPLIB:`"$implib`" " +
       "/SUBSYSTEM:WINDOWS,10.00 user32.lib gdi32.lib gdiplus.lib d3d11.lib dxgi.lib && " +
       "dumpbin /nologo /dependents `"$out`" > `"$dependencyReport`" && " +
       "dumpbin /nologo /headers `"$out`" > `"$headerReport`""

& $env:ComSpec /d /s /c $cmd
if ($LASTEXITCODE -ne 0) {
    throw "Native core build failed with exit code $LASTEXITCODE"
}

$dependencies = Get-Content -Raw -LiteralPath $dependencyReport
$headers = Get-Content -Raw -LiteralPath $headerReport
if ($dependencies -match "(?i)(VCRUNTIME|MSVCP|UCRTBASE|api-ms-win-crt)") {
    throw "Native core unexpectedly depends on a dynamic MSVC/UCRT runtime. /MT contract failed."
}
if ($headers -notmatch "(?i)8664 machine \(x64\)") {
    throw "Native core is not an x64 PE image."
}
if ($headers -notmatch "(?im)^\s*10\.00\s+subsystem version\s*$") {
    throw "Native core does not declare the Windows 10 subsystem baseline."
}

Write-Host "Built and PE-verified $out"
