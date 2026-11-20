[CmdletBinding()]
param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]] $ImageSha = @()
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Stop-Deployment([string] $Message) {
    [Console]::Error.WriteLine($Message)
    exit 1
}

# Validate arity and image identity before opening a lock or invoking Docker.
if ($ImageSha.Count -ne 1 -or $ImageSha[0] -notmatch '^[0-9a-fA-F]{40}$') {
    Stop-Deployment 'Usage: deploy-beta.ps1 <full-40-character-commit-sha>'
}
$sha = $ImageSha[0].ToLowerInvariant()

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$composeFile = Join-Path $repoRoot 'docker-compose.beta.yml'
$envFile = $env:COOKBOOK_BETA_ENV_FILE
$dataDir = $env:COOKBOOK_BETA_DATA_DIR
$publicUrl = $env:COOKBOOK_BETA_PUBLIC_URL
$port = $env:COOKBOOK_BETA_PORT
$proxyNetwork = $env:COOKBOOK_PROXY_NETWORK

if (-not $envFile -or -not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
    Stop-Deployment 'COOKBOOK_BETA_ENV_FILE must point to the protected beta env file.'
}
if (-not $dataDir -or -not [IO.Path]::IsPathRooted($dataDir)) {
    Stop-Deployment 'COOKBOOK_BETA_DATA_DIR must be an absolute beta-only path.'
}
$normalizedDataDir = [IO.Path]::GetFullPath($dataDir)
if ((Split-Path -Leaf $normalizedDataDir) -notmatch '(?i)^cookbook-beta-data$' -or
    $normalizedDataDir.StartsWith($repoRoot, [StringComparison]::OrdinalIgnoreCase)) {
    Stop-Deployment 'COOKBOOK_BETA_DATA_DIR must be an external directory named cookbook-beta-data.'
}
if ((Split-Path -Leaf $envFile) -notmatch '(?i)^(?:cookbook-beta\.env|\.env\.beta)$') {
    Stop-Deployment 'COOKBOOK_BETA_ENV_FILE must be a beta-specific env file named cookbook-beta.env or .env.beta.'
}
if (-not $publicUrl -or -not $publicUrl.StartsWith('https://', [StringComparison]::OrdinalIgnoreCase)) {
    Stop-Deployment 'COOKBOOK_BETA_PUBLIC_URL must be the configured HTTPS beta route.'
}
if (-not $port -or $port -notmatch '^\d{1,5}$' -or [int]$port -lt 1 -or [int]$port -gt 65535) {
    Stop-Deployment 'COOKBOOK_BETA_PORT must be a valid TCP port.'
}
if (-not $proxyNetwork) {
    Stop-Deployment 'COOKBOOK_PROXY_NETWORK must name the existing reverse-proxy network.'
}
if (-not (Test-Path -LiteralPath $composeFile -PathType Leaf)) {
    Stop-Deployment 'Beta Compose configuration is missing.'
}

# Serialize on the Windows runner. FileShare.None keeps the lock across
# processes and releases it automatically if this process exits unexpectedly.
$lockPath = Join-Path $dataDir '.deploy-beta.lock'
try {
    [IO.Directory]::CreateDirectory($dataDir) | Out-Null
    $lock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
} catch {
    Stop-Deployment 'Another beta deployment holds the lock, or the beta data directory is unavailable.'
}

$composeArgs = @('--project-name', 'cookbook-beta', '--env-file', $envFile, '-f', $composeFile)
# Pin the non-secret host settings to the exact values already validated above.
# Process environment values override same-named entries in the Compose env file,
# keeping the lock, backup and readiness port aligned with the resolved stack.
$env:COOKBOOK_BETA_DATA_DIR = $dataDir
$env:COOKBOOK_BETA_PORT = $port
$env:COOKBOOK_PROXY_NETWORK = $proxyNetwork
function Invoke-BetaCompose([string[]] $Arguments) {
    & docker compose @composeArgs @Arguments
    if ($LASTEXITCODE -ne 0) { throw "docker compose failed with exit code $LASTEXITCODE" }
}
function Test-HttpOk([string] $Url) {
    try {
        $response = Invoke-WebRequest -Uri $Url -Method Get -TimeoutSec 8 -UseBasicParsing
        return [int]$response.StatusCode -ge 200 -and [int]$response.StatusCode -lt 400
    } catch { return $false }
}

$oldImage = $null
$appWasStopped = $false
$backupPath = $null
try {
    $env:COOKBOOK_IMAGE = 'ghcr.io/bjmaynard01/cookbook:sha-' + $sha

    # Ensure the environment file and required Compose resources resolve before
    # changing the running app. Do not print the expanded config (it contains secrets).
    Invoke-BetaCompose @('config', '--quiet')
    $appId = (& docker compose @composeArgs 'ps' '-q' 'app').Trim()
    if ($LASTEXITCODE -ne 0 -or -not $appId) { throw 'The beta app service is not provisioned.' }
    $dbId = (& docker compose @composeArgs 'ps' '-q' 'db').Trim()
    if ($LASTEXITCODE -ne 0 -or -not $dbId) { throw 'The beta database service is not provisioned.' }
    $dbState = (& docker inspect --format '{{.State.Health.Status}}' $dbId).Trim()
    if ($LASTEXITCODE -ne 0 -or $dbState -ne 'healthy') { throw 'The beta database is not healthy.' }
    $appState = (& docker inspect --format '{{.State.Health.Status}}' $appId).Trim()
    if ($LASTEXITCODE -ne 0 -or $appState -ne 'healthy') { throw 'The current beta app is not healthy; recover it before deploying.' }

    $oldImage = (& docker inspect --format '{{.Config.Image}}' $appId).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $oldImage) { throw 'Could not record the current beta image.' }
    [Console]::WriteLine("Current beta image: $oldImage")

    Invoke-BetaCompose @('stop', 'app')
    $appWasStopped = $true

    # Use the already-running image for backup tooling. The deploy candidate
    # is selected only after the paired backup has been verified.
    $env:COOKBOOK_IMAGE = $oldImage
    $backupId = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '-' + [Guid]::NewGuid().ToString('N')
    $backupPath = Join-Path $dataDir (Join-Path 'backups' $backupId)
    [IO.Directory]::CreateDirectory($backupPath) | Out-Null
    $containerBackupPath = '/backups/' + $backupId
    Invoke-BetaCompose @('run', '--rm', '--no-deps', '--entrypoint', 'cookbook', 'app', 'backup', '--out', $containerBackupPath)
    $manifest = @(Get-ChildItem -LiteralPath $backupPath -Filter 'manifest-*.json' -File)
    $sql = @(Get-ChildItem -LiteralPath $backupPath -Filter 'cookbook-*.sql.gz' -File)
    $media = @(Get-ChildItem -LiteralPath $backupPath -Filter 'media-*.tar.gz' -File)
    if ($manifest.Count -ne 1 -or $sql.Count -ne 1 -or $media.Count -ne 1) {
        throw "Backup artifact verification failed; retain artifacts for inspection: $backupPath"
    }
    [Console]::WriteLine("Verified paired backup: $backupPath")

    $env:COOKBOOK_IMAGE = 'ghcr.io/bjmaynard01/cookbook:sha-' + $sha
    Invoke-BetaCompose @('pull', 'app')
    Invoke-BetaCompose @('up', '-d', '--no-deps', 'app')

    $localUrl = "http://127.0.0.1:$port"
    $ready = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        if (Test-HttpOk "$localUrl/readyz") { $ready = $true; break }
        Start-Sleep -Seconds 2
    }
    if (-not $ready) { throw 'Beta app did not become ready.' }
    if (-not (Test-HttpOk "$localUrl/healthz")) { throw 'Beta app health check failed.' }
    if (-not (Test-HttpOk $publicUrl)) { throw 'Configured public beta route did not respond successfully.' }

    $appWasStopped = $false
    [Console]::WriteLine("Beta deployment succeeded: $sha")
} catch {
    if ($appWasStopped) {
        # Keep beta offline after any post-stop failure. Never automatically
        # restore over a possibly migrated database or media directory.
        try { Invoke-BetaCompose @('stop', 'app') } catch { }
    }
    [Console]::Error.WriteLine("Beta deployment failed. Previous image: $oldImage; backup: $backupPath")
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
} finally {
    if ($null -ne $lock) { $lock.Dispose() }
}
