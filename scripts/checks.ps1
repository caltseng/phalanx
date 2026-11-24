param(
    [string]$EnvironmentRoot
)

$ErrorActionPreference = 'Stop'
$Command = $args
if (-not $EnvironmentRoot -or -not $Command) {
    throw 'usage: checks.ps1 -EnvironmentRoot <prepared environment> <command> [args...]'
}
$envRoot = (Resolve-Path -LiteralPath $EnvironmentRoot).Path
$expected = [IO.File]::ReadAllText((Join-Path $envRoot 'lock.sha256')).Trim().ToLowerInvariant()
if ($expected -notmatch '^[0-9a-f]{64}$') {
    throw 'checks environment has an invalid lock.sha256'
}
$lock = Join-Path (Get-Location) 'package-lock.json'
if (-not (Test-Path -LiteralPath $lock -PathType Leaf)) {
    throw 'checks env stale: package-lock.json is missing'
}
$text = [IO.File]::ReadAllText($lock).Replace("`r`n", "`n")
$sha = [Security.Cryptography.SHA256]::Create()
try {
    $actual = -join ($sha.ComputeHash([Text.UTF8Encoding]::new($false).GetBytes($text)) |
        ForEach-Object { $_.ToString('x2') })
} finally {
    $sha.Dispose()
}
if ($actual -ne $expected) {
    throw 'checks env stale: package-lock.json differs from the provisioned env'
}
$target = Join-Path $envRoot 'node_modules'
if (-not (Test-Path -LiteralPath $target -PathType Container)) {
    throw 'checks environment is missing node_modules; ask the operator to provision it'
}
$modules = Join-Path (Get-Location) 'node_modules'
if (Test-Path -LiteralPath $modules) {
    $item = Get-Item -LiteralPath $modules -Force
    if (-not $item.Target -or
        [IO.Path]::GetFullPath([string]$item.Target) -ne [IO.Path]::GetFullPath($target)) {
        throw 'existing node_modules does not point to the prepared environment; refusing to replace it'
    }
} else {
    New-Item -ItemType Junction -Path $modules -Target $target | Out-Null
}
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $envRoot 'ms-playwright'
$exe = $Command[0]
$rest = @($Command | Select-Object -Skip 1)
# Windows PowerShell must not turn native stderr into a terminating error.
$ErrorActionPreference = 'Continue'
& $exe @rest
if (-not $?) {
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    exit 1
}
exit $LASTEXITCODE
