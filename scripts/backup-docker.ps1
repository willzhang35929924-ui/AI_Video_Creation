#requires -Version 5.1
[CmdletBinding()]
param([string]$Destination = 'backups', [string]$ComposeFile = 'compose.yaml', [string]$ProjectName = '', [string]$HelperImage = '')
. (Join-Path $PSScriptRoot 'docker-common.ps1')
Push-Location $script:DockerProjectRoot
$backend = ''; $frontend = ''; $backendStopped = $false; $frontendStopped = $false; $partial = ''
try {
    $backend = Get-DockerServiceContainer -Service backend -ComposeFile $ComposeFile -ProjectName $ProjectName
    $frontend = Get-DockerServiceContainer -Service frontend -ComposeFile $ComposeFile -ProjectName $ProjectName
    if (-not $backend) { throw 'No existing backend container. Nothing has been backed up.' }
    # Resolve once to an immutable, locally tested image BEFORE pausing ingress.
    $archiveImage = Resolve-DockerArchiveImage -Container $backend -HelperImage $HelperImage
    $backendRunning = Get-DockerContainerRunning $backend
    $frontendRunning = Get-DockerContainerRunning $frontend
    $destinationRoot = [System.IO.Path]::GetFullPath($Destination)
    $null = New-Item -ItemType Directory -Path $destinationRoot -Force
    $stamp = [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmssZ')
    $archive = Join-Path $destinationRoot ("video-factory-$stamp-$([guid]::NewGuid().ToString('N').Substring(0,8)).tar.gz")
    $partial = $archive + '.partial'
    if ($frontendRunning) {
        $frontendStopped = $true
        Invoke-DockerChecked -DockerArguments @('stop', '--timeout', '30', $frontend)
    }
    if ($backendRunning) {
        Invoke-DockerChecked -DockerArguments (Get-DockerArchiveArguments $backend 'check-idle' $archiveImage)
        $backendStopped = $true
        Invoke-DockerChecked -DockerArguments @('stop', '--timeout', '60', $backend)
    }
    Invoke-DockerBinaryTransfer -DockerArguments (Get-DockerArchiveArguments $backend 'backup' $archiveImage) -File $partial
    Move-Item -LiteralPath $partial -Destination $archive
    $partial = ''
    $hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    [System.IO.File]::WriteAllText($archive + '.sha256', "$hash  $([System.IO.Path]::GetFileName($archive))`n", (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "Private backup complete: $archive"
    Write-Host 'Includes encrypted credentials and their master key. Keep the archive and checksum private; never upload them to GitHub.'
} finally {
    # Restart exact existing containers; never pull images, recreate containers or delete volumes.
    $restartErrors = @()
    if ($backendStopped) {
        & docker start $backend | Out-Host
        if ($LASTEXITCODE -ne 0) { $restartErrors += 'backend' }
    }
    if ($frontendStopped) {
        & docker start $frontend | Out-Host
        if ($LASTEXITCODE -ne 0) { $restartErrors += 'frontend' }
    }
    if ($partial -and (Test-Path -LiteralPath $partial -PathType Leaf)) { Remove-Item -LiteralPath $partial }
    Pop-Location
    if ($restartErrors.Count) { throw ('Failed to restart existing containers: ' + ($restartErrors -join ', ') + '. Data volumes have been preserved. Start Docker Desktop and restart the same containers.') }
}
