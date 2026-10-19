[CmdletBinding()]
param(
    [switch]$NoPublish,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$BootstrapArguments
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$pythonExecutable = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
    throw ([regex]::Unescape('\u8bf7\u5148\u6267\u884c scripts/bootstrap/start.ps1 --web \u5b8c\u6210\u9996\u6b21\u73af\u5883\u5b89\u88c5\u548c\u955c\u50cf\u6784\u5efa\u3002'))
}

$env:TRACEFIX_PROJECTS = Join-Path $projectRoot '.tracefix/interface-projects.yaml'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$repositoryRoot = Join-Path $projectRoot '.tracefix/repositories'
if ($null -eq $env:TRACEFIX_REPOSITORY_ROOTS) {
    $env:TRACEFIX_REPOSITORY_ROOTS = ConvertTo-Json -InputObject @($repositoryRoot) -Compress
}
if ($null -eq $env:TRACEFIX_SERVICE_GITHUB_CREDENTIAL_MAP) {
    $env:TRACEFIX_SERVICE_GITHUB_CREDENTIAL_MAP = '{"deployment/test-bug":"TRACEFIX_GITHUB_TOKEN"}'
}
$registrationArguments = @(
    (Join-Path $projectRoot 'scripts/github/configure_test_repository.py'),
    '--registry', $env:TRACEFIX_PROJECTS, '--ensure-existing'
)
if (-not $NoPublish) {
    $registrationArguments += '--publish-branch'
}

Push-Location $projectRoot
try {
    & $pythonExecutable @registrationArguments
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
    & (Join-Path $PSScriptRoot 'start.ps1') --web --skip-install --skip-build @BootstrapArguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
