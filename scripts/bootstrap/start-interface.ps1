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
    throw '请先执行 scripts/bootstrap/start.ps1 --web 完成首次环境安装和镜像构建。'
}

$env:TRACEFIX_PROJECTS = Join-Path $projectRoot '.tracefix/interface-projects.yaml'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$repositoryRoot = Join-Path $projectRoot '.tracefix/repositories'
$env:TRACEFIX_REPOSITORY_ROOTS = ConvertTo-Json -InputObject @($repositoryRoot) -Compress
$env:TRACEFIX_SERVICE_GITHUB_CREDENTIAL_MAP = '{"deployment/test-bug":"TRACEFIX_GITHUB_TOKEN"}'
$registrationArguments = @(
    (Join-Path $projectRoot 'scripts/github/configure_test_repository.py'),
    '--registry', $env:TRACEFIX_PROJECTS
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
