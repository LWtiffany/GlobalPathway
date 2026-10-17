$ErrorActionPreference = 'Stop'
$pidPath = Join-Path $env:LOCALAPPDATA 'Team03PgTunnel\ssh.pid'
if (-not (Test-Path $pidPath)) {
    Write-Output 'No saved Team 03 tunnel PID was found.'
    exit 0
}

$tunnelPid = [int](Get-Content $pidPath -Raw)
$process = Get-Process -Id $tunnelPid -ErrorAction SilentlyContinue
if ($process -and $process.ProcessName -eq 'ssh') {
    Stop-Process -Id $tunnelPid
    Write-Output "Stopped Team 03 PostgreSQL tunnel (PID $tunnelPid)."
}
else {
    Write-Output 'The saved tunnel process is no longer running.'
}
Remove-Item $pidPath -Force
