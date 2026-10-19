# Windows PowerShell 5.1 / PowerShell 7. Arguments are forwarded unchanged.
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $Utf8NoBom
[Console]::OutputEncoding = $Utf8NoBom
$OutputEncoding = $Utf8NoBom
$ErrorActionPreference = 'Stop'
$TraceFixArguments = @($args)
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$PythonCommand = $null
$PythonPrefix = @()
if (Get-Command py.exe -ErrorAction SilentlyContinue) {
    try {
        & py.exe -3.12 -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $PythonCommand = 'py.exe'
            $PythonPrefix = @('-3.12')
        }
    } catch { }
}
if (-not $PythonCommand -and (Get-Command python.exe -ErrorAction SilentlyContinue)) {
    try {
        & python.exe -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $PythonCommand = 'python.exe' }
    } catch { }
}
if (-not $PythonCommand) {
    Write-Host '需要 Python 3.12（64 位）。请从 python.org 安装后重新打开终端。'
    exit 2
}
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
Push-Location -LiteralPath $ProjectRoot
try {
    & $PythonCommand @PythonPrefix (Join-Path $PSScriptRoot 'bootstrap.py') @TraceFixArguments
    $TraceFixExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $TraceFixExitCode
