param(
    [ValidateNotNullOrEmpty()]
    [string]$Message = 'feat: add unified web startup and workspace configuration',
    [switch]$Preview
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$CommitFiles = @(
    'start-web.cmd'
    'start-windows.cmd'
    'tools/bootstrap/bootstrap.py'
    'tools/bootstrap/services.py'
    'package.json'
    'package-lock.json'
    'pyproject.toml'
    'evals/cases.py'
)

Push-Location -LiteralPath $ProjectRoot
try {
    foreach ($File in $CommitFiles) {
        if (-not (Test-Path -LiteralPath $File -PathType Leaf)) {
            throw "Required file is missing: $File"
        }
    }

    & git status --short -- @CommitFiles
    if ($LASTEXITCODE -ne 0) { throw 'Unable to read Git status.' }

    if ($Preview) {
        Write-Host 'Preview only. No files staged or committed.'
        return
    }

    & git add -- @CommitFiles
    if ($LASTEXITCODE -ne 0) { throw 'Git add failed. No commit was created.' }

    & git diff --cached --quiet -- @CommitFiles
    $DiffExitCode = $LASTEXITCODE
    if ($DiffExitCode -eq 0) {
        Write-Host 'No changes to commit in the selected files.'
        return
    }
    if ($DiffExitCode -ne 1) { throw 'Unable to inspect staged changes.' }

    & git commit --only -m $Message -- @CommitFiles
    if ($LASTEXITCODE -ne 0) {
        throw 'Git commit failed. Selected changes remain staged for inspection.'
    }

    & git show --stat --oneline HEAD
    if ($LASTEXITCODE -ne 0) { throw 'Commit created, but unable to display its summary.' }
} finally {
    Pop-Location
}
