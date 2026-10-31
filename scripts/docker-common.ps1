#requires -Version 5.1
# Docker-only helpers. Do not source the native common.ps1 or print .env values.
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$script:DockerProjectRoot = Split-Path -Parent $PSScriptRoot

function Invoke-DockerChecked {
    param([Parameter(Mandatory = $true)][string[]]$DockerArguments)
    & docker @DockerArguments
    if ($LASTEXITCODE -ne 0) { throw "Docker command failed (exit $LASTEXITCODE)." }
}

function Get-DockerServiceContainer {
    param([string]$Service, [string]$ComposeFile, [string]$ProjectName)
    if ($ProjectName) {
        $result = @(& docker ps --all --quiet --filter "label=com.docker.compose.project=$ProjectName" --filter "label=com.docker.compose.service=$Service")
    } else {
        $result = @(& docker compose -f $ComposeFile ps --all --quiet $Service)
    }
    if ($LASTEXITCODE -ne 0) { throw "Cannot find Compose containers. For an old deployment, pass -ProjectName with its existing Compose project name." }
    $result = @($result | Where-Object { $_ -and $_.Trim() })
    if ($result.Count -gt 1) { throw "Multiple $Service containers found; this SQLite deployment requires exactly one backend." }
    if ($result.Count -eq 0) { return "" }
    return $result[0].Trim()
}

function Get-DockerContainerRunning {
    param([string]$Container)
    if (-not $Container) { return $false }
    $value = & docker inspect --format '{{.State.Running}}' $Container
    if ($LASTEXITCODE -ne 0) { throw "Cannot inspect the selected container." }
    return $value -eq "true"
}

function Resolve-DockerArchiveImage {
    param([string]$Container, [string]$HelperImage = '')
    $usage = 'Pass -HelperImage with an explicitly reviewed, locally installed compatible backend image. No image is pulled and no service has been stopped.'
    $reference = $HelperImage
    if (-not $reference) {
        $reference = & docker inspect --format '{{.Image}}' $Container
        if ($LASTEXITCODE -ne 0 -or -not $reference) { throw "Cannot resolve the existing backend image. $usage" }
    }
    try {
        $metadata = & docker image inspect --format '{{.Id}} {{.Os}} {{.Architecture}}' $reference.Trim() 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $metadata) { throw 'Image is not present locally.' }
    } catch { throw "Backup helper image is unavailable locally. A running container may outlive its removed image. $usage" }
    $parts = ($metadata -join ' ').Trim() -split '\s+'
    if ($parts.Count -ne 3 -or $parts[0] -notmatch '^sha256:[a-f0-9]{64}$' -or $parts[1] -ne 'linux' -or $parts[2] -notin @('amd64', 'arm64')) {
        throw "Backup helper must be a Linux AMD64 or ARM64 image. $usage"
    }
    $engine = & docker version --format '{{.Server.Os}}/{{.Server.Arch}}'
    if ($LASTEXITCODE -ne 0 -or $engine -notmatch '^linux/(amd64|arm64)$') { throw "A reachable Linux Docker engine is required. $usage" }
    $helper = Join-Path $script:DockerProjectRoot 'docker\volume_archive.py'
    if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) { throw 'Missing public docker/volume_archive.py helper.' }
    $mount = 'type=bind,"source=' + $helper + '",target=/opt/volume_archive.py,readonly'
    try {
        # No business mounts: probe the actual CLI and Python stdlib without
        # starting the application, accessing credentials, or writing a volume.
        $helpText = & docker run --rm --pull never --network none --read-only --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges:true --mount $mount --entrypoint python $parts[0] /opt/volume_archive.py --help 2>$null
        if ($LASTEXITCODE -ne 0) { throw 'Isolated helper probe failed.' }
        $helpText = $helpText -join "`n"
        foreach ($command in @('check-idle', 'check-empty', 'backup', 'restore')) {
            if (-not $helpText.Contains($command)) { throw 'Required archive command is missing.' }
        }
    } catch { throw "Backup helper could not run the required archive CLI on this Docker engine. $usage" }
    Write-Host "Backup helper validated: $($parts[0]) ($($parts[1])/$($parts[2])); engine $engine."
    return $parts[0]
}

function Get-DockerArchiveArguments {
    param([string]$Container, [string]$Command, [string]$HelperImage = '')
    $image = $HelperImage
    if (-not $image) {
        $image = & docker inspect --format '{{.Image}}' $Container
        if ($LASTEXITCODE -ne 0 -or -not $image) { throw "Cannot resolve the exact existing backend image." }
    }
    $helper = Join-Path $script:DockerProjectRoot 'docker\volume_archive.py'
    if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) { throw "Missing public docker/volume_archive.py helper." }
    # CSV quoting supports project paths with spaces or commas, including Windows drives.
    $mount = 'type=bind,"source=' + $helper + '",target=/opt/volume_archive.py,readonly'
    return @('run', '--rm', '--pull', 'never', '--network', 'none', '--read-only', '--user', '10001:10001',
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true', '--volumes-from', $Container,
        '--mount', $mount, '--entrypoint', 'python', $image.Trim(), '/opt/volume_archive.py', $Command)
}

function ConvertTo-DockerProcessArgument {
    param([AllowEmptyString()][string]$Value)
    if ($Value -and $Value -notmatch '[\s"]') { return $Value }
    # CommandLineToArgvW escaping. JSON/string interpolation is not shell quoting.
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Invoke-DockerBinaryTransfer {
    param([string[]]$DockerArguments, [string]$File, [switch]$InputFile)
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = (Get-Command docker -ErrorAction Stop).Source
    $info.Arguments = (($DockerArguments | ForEach-Object { ConvertTo-DockerProcessArgument $_ }) -join ' ')
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardError = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardInput = [bool]$InputFile
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    $stream = $null
    $started = $false
    try {
        $null = $process.Start()
        $started = $true
        $errorTask = $process.StandardError.ReadToEndAsync()
        if ($InputFile) {
            $outputTask = $process.StandardOutput.ReadToEndAsync()
            $stream = [System.IO.File]::OpenRead($File)
            $stream.CopyTo($process.StandardInput.BaseStream)
            $process.StandardInput.Close()
        } else {
            # PowerShell 5.1 '>' corrupts binary stdout; copy raw bytes instead.
            $stream = [System.IO.File]::Open($File, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write)
            $process.StandardOutput.BaseStream.CopyTo($stream)
        }
        $stream.Dispose(); $stream = $null
        $process.WaitForExit()
        $errorText = $errorTask.GetAwaiter().GetResult()
        if ($errorText) { Write-Host $errorText.TrimEnd() }
        if ($InputFile) {
            $outputText = $outputTask.GetAwaiter().GetResult()
            if ($outputText) { Write-Host $outputText.TrimEnd() }
        }
        if ($process.ExitCode -ne 0) { throw "Backup/restore helper failed (exit $($process.ExitCode))." }
    } finally {
        if ($stream) { $stream.Dispose() }
        if ($started -and -not $process.HasExited) { $process.Kill() }
        $process.Dispose()
    }
}
