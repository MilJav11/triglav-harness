param([ValidateSet('review-safe', 'review-performance')][string]$ReviewProfile)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$binary = Join-Path $root 'tools\llama-swap\bin\llama-swap.exe'
$config = Join-Path $root 'runs\swap-profile.yaml'
$runDir = Join-Path $root 'runs'
$pidFile = Join-Path $runDir 'swap.pid'
if (-not (Test-Path -LiteralPath $binary)) { throw "Missing $binary; see RUNBOOK.md" }
if (Test-Path -LiteralPath $pidFile) {
    $recorded = [int](Get-Content -LiteralPath $pidFile -Raw)
    $existing = Get-Process -Id $recorded -ErrorAction SilentlyContinue
    if ($existing) { throw "llama-swap PID $recorded is already running" }
}
$portProbe = [System.Net.Sockets.TcpClient]::new()
try {
    $portProbe.Connect('127.0.0.1', 9292)
    throw 'Port 9292 is already in use; inspect it before starting another instance'
} catch [System.Net.Sockets.SocketException] {
    # No listener owns the port.
} finally {
    $portProbe.Dispose()
}
New-Item -ItemType Directory -Force -Path $runDir | Out-Null
if ($ReviewProfile) { & python (Join-Path $root 'scripts\render-swap.py') --profile $ReviewProfile }
else { & python (Join-Path $root 'scripts\render-swap.py') }
if ($LASTEXITCODE -ne 0) { throw 'Could not render reviewer profile' }
& $binary -config $config -validate
if ($LASTEXITCODE -ne 0) { throw 'Invalid llama-swap config' }
$process = Start-Process -FilePath $binary -ArgumentList '-config', ('"' + $config + '"'), '-listen', '127.0.0.1:9292' `
    -WorkingDirectory $root -RedirectStandardOutput (Join-Path $runDir 'swap.stdout.log') `
    -RedirectStandardError (Join-Path $runDir 'swap.stderr.log') -WindowStyle Hidden -PassThru
$process.Id | Set-Content -LiteralPath $pidFile
Start-Sleep -Seconds 2
try { Invoke-RestMethod 'http://127.0.0.1:9292/health' -TimeoutSec 5 | Out-Null }
catch { throw "llama-swap PID $($process.Id) did not become healthy; inspect runs/swap.stderr.log" }
Write-Output "llama-swap running on 127.0.0.1:9292 (PID $($process.Id))"
