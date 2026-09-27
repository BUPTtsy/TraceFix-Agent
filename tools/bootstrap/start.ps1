# Windows PowerShell 5.1 / PowerShell 7. Arguments are forwarded unchanged.
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
    Write-Host ([regex]::Unescape('\u9700\u8981 Python 3.12\uff0864 \u4f4d\uff09\u3002\u8bf7\u4ece python.org \u5b89\u88c5\u540e\u91cd\u65b0\u6253\u5f00\u7ec8\u7aef\u3002'))
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
