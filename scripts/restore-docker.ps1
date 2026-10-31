#requires -Version 5.1
[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$Archive, [string]$ComposeFile = 'compose.yaml', [string]$ProjectName = '')
. (Join-Path $PSScriptRoot 'docker-common.ps1')
$archivePath = (Resolve-Path -LiteralPath $Archive).Path
$checksumPath = $archivePath + '.sha256'
if (-not (Test-Path -LiteralPath $checksumPath -PathType Leaf)) { throw 'Missing .sha256 sidecar; refuse an unverified restore.' }
$expected = ((Get-Content -LiteralPath $checksumPath -Raw).Trim() -split '\s+')[0]
if ($expected -notmatch '^[a-fA-F0-9]{64}$' -or (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash -ne $expected) {
    throw 'Archive SHA256 does not match. Nothing has been restored.'
}
Push-Location $script:DockerProjectRoot
try {
    $backend = Get-DockerServiceContainer -Service backend -ComposeFile $ComposeFile -ProjectName $ProjectName
    $frontend = Get-DockerServiceContainer -Service frontend -ComposeFile $ComposeFile -ProjectName $ProjectName
    if ((Get-DockerContainerRunning $backend) -or (Get-DockerContainerRunning $frontend)) {
        throw 'Restore requires stopped, empty target containers. Choose a NEW unused COMPOSE_PROJECT_NAME; existing data will never be overwritten.'
    }
    if (-not $backend) {
        $create = @('compose', '-f', $ComposeFile)
        if ($ProjectName) { $create += @('-p', $ProjectName) }
        Invoke-DockerChecked -DockerArguments ($create + @('create'))
        $backend = Get-DockerServiceContainer -Service backend -ComposeFile $ComposeFile -ProjectName $ProjectName
    }
    if (-not $backend) { throw 'Could not create the empty backend target.' }
    Invoke-DockerChecked -DockerArguments (Get-DockerArchiveArguments $backend 'check-empty')
    $transfer = Get-DockerArchiveArguments $backend 'restore'
    # docker run needs stdin kept open to receive the raw archive stream.
    $transfer = @($transfer[0], '-i') + $transfer[1..($transfer.Length - 1)]
    Invoke-DockerBinaryTransfer -DockerArguments $transfer -File $archivePath -InputFile
    Write-Host 'Restore complete. Start the same Compose project normally; any required migrations run automatically.'
} finally { Pop-Location }
