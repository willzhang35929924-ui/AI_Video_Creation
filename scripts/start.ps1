#requires -Version 5.1
[CmdletBinding()]
param([switch]$Development)
. (Join-Path $PSScriptRoot 'docker-common.ps1')
$composeFile = if ($Development) { 'compose.dev.yaml' } else { 'compose.yaml' }
Push-Location $script:DockerProjectRoot
try {
    $engineOs = & docker info --format '{{.OSType}}'
    if ($LASTEXITCODE -ne 0) { throw 'Start Docker Desktop and wait for its engine to be ready, then retry.' }
    if ($engineOs -ne 'linux') { throw 'Switch Docker Desktop to Linux containers. Windows containers cannot run these application images.' }
    Invoke-DockerChecked -DockerArguments @('compose', '-f', $composeFile, 'up', '-d', '--wait', '--wait-timeout', '180')
    $address = & docker compose -f $composeFile port frontend 8080
    if ($LASTEXITCODE -eq 0) { Write-Host ('Open http://' + ($address -replace '^0\.0\.0\.0:', '127.0.0.1:')) }
} finally { Pop-Location }
