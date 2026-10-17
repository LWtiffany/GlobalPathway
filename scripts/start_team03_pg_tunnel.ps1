param(
    [int]$LocalPort = 15432
)

$ErrorActionPreference = 'Stop'
$keyPath = Join-Path $env:USERPROFILE '.ssh\team03_ecs_tunnel'
$stateDir = Join-Path $env:LOCALAPPDATA 'Team03PgTunnel'
$pidPath = Join-Path $stateDir 'ssh.pid'

if (-not (Test-Path $keyPath)) {
    throw "SSH key not found: $keyPath"
}
New-Item -ItemType Directory -Path $stateDir -Force | Out-Null

if (Test-Path $pidPath) {
    $existingPid = [int](Get-Content $pidPath -Raw)
    $existing = Get-Process -Id $existingPid -ErrorAction SilentlyContinue
    if ($existing -and $existing.ProcessName -eq 'ssh') {
        Write-Output "Tunnel is already running (PID $existingPid), localhost:$LocalPort"
        exit 0
    }
    Remove-Item $pidPath -Force
}

$sshArgs = @(
    '-N',
    '-L', "127.0.0.1:$LocalPort`:127.0.0.1:15432",
    '-i', $keyPath,
    '-o', 'IdentitiesOnly=yes',
    '-o', 'ExitOnForwardFailure=yes',
    '-o', 'ServerAliveInterval=30',
    '-o', 'ServerAliveCountMax=3',
    'root@123.56.13.78'
)
$process = Start-Process -FilePath 'ssh.exe' -ArgumentList $sshArgs -WindowStyle Hidden -PassThru
Set-Content -Path $pidPath -Value $process.Id -NoNewline

$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 300
    $process.Refresh()
    if ($process.HasExited) {
        Remove-Item $pidPath -Force -ErrorAction SilentlyContinue
        throw "SSH tunnel exited early (code $($process.ExitCode)). Check ECS SSH access and the remote forwarder service."
    }
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $client.Connect('127.0.0.1', $LocalPort)
        $ready = $true
        break
    }
    catch {
        # Keep checking while ssh establishes the tunnel.
    }
    finally {
        $client.Dispose()
    }
}

if (-not $ready) {
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    Remove-Item $pidPath -Force -ErrorAction SilentlyContinue
    throw "Tunnel did not become ready on localhost:$LocalPort."
}

Write-Output "PostgreSQL tunnel is ready at 127.0.0.1:$LocalPort (PID $($process.Id))."
Write-Output ("Use host=127.0.0.1 port={0} database=team03 user=team03." -f $LocalPort)
