$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pidFile = Join-Path $root 'runs\swap.pid'
if (-not (Test-Path -LiteralPath $pidFile)) { throw 'No runs/swap.pid found' }
$recorded = [int](Get-Content -LiteralPath $pidFile -Raw)
$process = Get-Process -Id $recorded -ErrorAction SilentlyContinue
if (-not $process) { throw "Recorded PID $recorded is not running; inspect and remove stale PID file manually" }
$expected = (Resolve-Path (Join-Path $root 'tools\llama-swap\bin\llama-swap.exe')).Path
if ($process.Path -ne $expected) { throw "PID $recorded is not the expected llama-swap binary" }
& python (Join-Path $root 'harness.py') unload
if ($LASTEXITCODE -ne 0) { throw 'Controller could not confirm process exit and RAM release; leaving gateway running' }
try {
    Stop-Process -Id $recorded -ErrorAction Stop
} catch {
    # A process started under the sandbox account may require taskkill here.
    & taskkill.exe /PID $recorded /F | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not stop llama-swap PID $recorded" }
}
if (-not $process.WaitForExit(10000)) { throw 'llama-swap did not exit' }
$process.Dispose()
Remove-Item -LiteralPath $pidFile
Write-Output 'llama-swap stopped; no models running'
