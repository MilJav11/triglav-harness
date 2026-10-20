<#
.SYNOPSIS
    Invoke-Triglav — Practical PowerShell Operator Wrapper for TRIGLAV Local AI Harness.

.DESCRIPTION
    Delegates directly to the reviewed TRIGLAV Phase 3.1 controller (harness.py).
    Enforces repository cleanliness, strict file policy scoping, native Windows Job
    containment, WAL persistence, and supervised gateway lifecycle.

    Supports both single-model mode (Qwen3-Coder-30B) and the complete three-model
    chain (Qwen executor -> Nemotron critic -> GPT-OSS 120B HotPin senior reviewer).

    NOTE ON FILE CREATION:
    The SafeQwen executor operates strictly via diff replacements on existing files.
    All paths in -AllowPaths must already exist at baseline. Dynamic file creation is
    deferred to a future release.

.PARAMETER Repo
    Path to the target Git repository. Must be a clean, committed Git working copy.

.PARAMETER Task
    Natural-language instruction describing the objective to accomplish.

.PARAMETER Verify
    Approved deterministic verification command (e.g. 'python -m unittest test_calculator.py').

.PARAMETER AllowPaths
    Array of relative file paths within the target repo that the AI is permitted to edit.
    Every file must already exist in the target repository at baseline.

.PARAMETER ForbidPaths
    Array of relative file paths within the target repo that are frozen/read-only (e.g. tests).

.PARAMETER ThreeModel
    Enable the full three-model pipeline (Qwen executor -> Nemotron critic -> GPT-OSS reviewer).
    Requires at least 44 GiB free physical RAM for HotPin reviewer admission.

.PARAMETER DryRun
    Validate inputs, file scope, budgets and policy without starting gateways or calling models.

.PARAMETER TriglavRoot
    Path to the TRIGLAV release working tree (default: auto-detected or 'C:\AI\worktrees\triglav-release').

.EXAMPLE
    # Dry-run validation of task and scope
    Invoke-Triglav -Repo "C:\my-project" -Task "Fix multiply" -Verify "python -m unittest" `
      -AllowPaths "calc.py" -ForbidPaths "test_calc.py", ".gitignore" -DryRun

.EXAMPLE
    # Single-model live coding run with Qwen
    Invoke-Triglav -Repo "C:\my-project" -Task "Fix multiply" -Verify "python -m unittest test_calc.py" `
      -AllowPaths "calc.py" -ForbidPaths "test_calc.py", ".gitignore"

.EXAMPLE
    # Full three-model gated run with trusted checkpoint
    Invoke-Triglav -Repo "C:\my-project" -Task "Fix multiply" -Verify "python -m unittest test_calc.py" `
      -AllowPaths "calc.py" -ForbidPaths "test_calc.py", ".gitignore" -ThreeModel
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory=$true, Position=0)]
    [string]$Repo,

    [Parameter(Mandatory=$true, Position=1)]
    [string]$Task,

    [Parameter(Mandatory=$true, Position=2)]
    [string]$Verify,

    [Parameter(Mandatory=$true)]
    [string[]]$AllowPaths,

    [Parameter(Mandatory=$true)]
    [string[]]$ForbidPaths,

    [switch]$ThreeModel,
    [switch]$DryRun,

    [string]$TriglavRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Auto-detect TriglavRoot if omitted
if (-not $TriglavRoot) {
    if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "..\harness.py"))) {
        $TriglavRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    } elseif (Test-Path "C:\AI\worktrees\triglav-release\harness.py") {
        $TriglavRoot = "C:\AI\worktrees\triglav-release"
    } else {
        $TriglavRoot = (Get-Location).Path
    }
}

$triglavPath = (Resolve-Path $TriglavRoot).Path
$harnessPy = Join-Path $triglavPath "harness.py"

if (-not (Test-Path $harnessPy)) {
    Write-Error "TRIGLAV harness.py not found at: $harnessPy"
    exit 1
}

$repoPath = (Resolve-Path $Repo).Path
if (-not (Test-Path (Join-Path $repoPath ".git"))) {
    Write-Error "Target repository is not a Git repository: $repoPath"
    exit 1
}

# 1. Require clean committed starting repository
Write-Host "=== TRIGLAV OPERATOR CONTROLLER ===" -ForegroundColor Cyan
Write-Host "Target Repository: $repoPath"
Write-Host "Task: $Task"
Write-Host "Mode: $(if ($ThreeModel) { 'Three-Model Chain (Qwen + Nemotron + GPT-OSS)' } else { 'Single-Model (Qwen)' })"
Write-Host "DryRun: $(if ($DryRun) { 'YES (no models loaded)' } else { 'NO (live inference)' })"

$gitStatus = (git -C $repoPath status --porcelain)
if ($gitStatus -and ($gitStatus | Out-String).Trim()) {
    Write-Error "Target repository has uncommitted changes or untracked files:`n$($gitStatus -join "`n")`nPlease commit or stash before running TRIGLAV."
    exit 1
}
Write-Host "[Preflight] Repository verified clean." -ForegroundColor Green

# 2. Validate file scope exists (Existing-file limitation)
foreach ($p in $AllowPaths) {
    $full = Join-Path $repoPath $p
    if (-not (Test-Path $full -PathType Leaf)) {
        Write-Error "Allowed path does not exist as a regular file in target repository: $p`nTRIGLAV SafeQwen executor requires editable files to already exist at baseline. Dynamic file creation is not currently supported."
        exit 1
    }
}
foreach ($p in $ForbidPaths) {
    $full = Join-Path $repoPath $p
    if (-not (Test-Path $full -PathType Leaf)) {
        Write-Error "Forbidden path does not exist as a regular file in target repository: $p"
        exit 1
    }
}
Write-Host "[Preflight] Target file scope validated." -ForegroundColor Green

# 3. Check physical RAM if live
$os = Get-CimInstance Win32_OperatingSystem
$freeRamGb = [math]::Round($os.FreePhysicalMemory / 1024 / 1024, 2)
Write-Host "[Preflight] Free physical RAM: $freeRamGb GiB"

if (-not $DryRun) {
    if ($ThreeModel -and $freeRamGb -lt 44.0) {
        Write-Error "Insufficient free RAM for ThreeModel ($freeRamGb GiB < 44.00 GiB required by HotPin reviewer policy). Aborting."
        exit 1
    }
    if (-not $ThreeModel -and $freeRamGb -lt 16.0) {
        Write-Error "Insufficient free RAM ($freeRamGb GiB < 16.00 GiB required for Qwen model load). Aborting."
        exit 1
    }
}

# 4. Prepare dedicated session directory (strictly outside target repo)
$timestamp = Get-Date -Format "yyyyMMddTHHmmssZ"
$sessionDir = "C:\AI\sessions\triglav-$timestamp"
New-Item -ItemType Directory -Path $sessionDir -Force | Out-Null

$policyPath = Join-Path $sessionDir "policy.json"
$yamlPath   = Join-Path $sessionDir "llama-swap.yaml"
$configPath = Join-Path $sessionDir "safeqwen.json"
$pidPath    = Join-Path $sessionDir "gateway.pid"
$stdoutLog  = Join-Path $sessionDir "gateway.stdout.log"
$stderrLog  = Join-Path $sessionDir "gateway.stderr.log"

function Set-Utf8NoBom($path, $text) {
    [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))
}

# Build policy.json
$readable = @($AllowPaths) + @($ForbidPaths) | Select-Object -Unique
$writable = @($AllowPaths) | Select-Object -Unique
$policyObj = @{
    readable             = @($readable)
    writable             = @($writable)
    mode                 = "mutation"
    max_actions          = 32
    unit_timeout_seconds = 600
}
Set-Utf8NoBom $policyPath ($policyObj | ConvertTo-Json -Depth 5)

# Build llama-swap.yaml
if ($ThreeModel) {
    $yamlContent = @"
healthCheckTimeout: 900
unloadTimeout: 30
globalTTL: 300
startPort: 5900
logToStdout: "proxy,upstream,http"
models:
  qwen-code:
    cmd: >-
      "C:/AI/runtimes/llama.cpp/bin/llama-server.exe" --model "C:/AI/models/Qwen3-Coder-30B-A3B/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf" --host 127.0.0.1 --port `${PORT} --ctx-size 32768 --threads 8 --flash-attn on --cache-type-k q8_0 --cache-type-v q8_0 --fit on --fit-target 768
    cmdStop: 'powershell.exe -NoProfile -NonInteractive -Command "Stop-Process -Id `${PID} -ErrorAction Stop"'
    ttl: 300
    checkEndpoint: /health
    capabilities:
      in: [text]
      out: [text]
      context: 32768
  nemotron-critic:
    cmd: >-
      "C:/AI/runtimes/llama.cpp/bin/llama-server.exe" --model "C:/AI/models/Nemotron-3.5-Lightning-30B-A3B/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-Q4_K_M.gguf" --host 127.0.0.1 --port `${PORT} --ctx-size 4096 --parallel 1 --threads 8 --threads-batch 8 --batch-size 256 --ubatch-size 128 --cpu-moe --gpu-layers auto --fit on --fit-target 1024 --spec-type none --reasoning on --reasoning-format deepseek --reasoning-budget 768 --offline --no-webui --no-agent --no-ui-mcp-proxy --no-mmproj
    cmdStop: 'powershell.exe -NoProfile -NonInteractive -Command "Stop-Process -Id `${PID} -ErrorAction Stop"'
    ttl: 300
    checkEndpoint: /health
    capabilities:
      in: [text]
      out: [text]
      context: 4096
  gptoss-review:
    cmd: >-
      "C:/AI/runtimes/hotpin/llama.cpp/build/bin/Release/llama-server.exe" --model "C:/AI/models/GPT-OSS-120B-Q4_K_M/gpt-oss-120b-Q4_K_M-00001-of-00002.gguf" --host 127.0.0.1 --port `${PORT} --ctx-size 8192 --threads 8 --flash-attn on --fit on --fit-target 768 --no-repack --mmap --verbosity 4
    cmdStop: 'powershell.exe -NoProfile -NonInteractive -Command "Stop-Process -Id `${PID} -ErrorAction Stop"'
    env:
      - "LLAMA_HOT_EXPERTS=C:/AI/repos/hotpin-llm/hot-files/gpt-oss-120b-q4km/hot_top4.txt"
    ttl: 300
    checkEndpoint: /health
    capabilities:
      in: [text]
      out: [text]
      context: 8192
"@
} else {
    $yamlContent = @"
healthCheckTimeout: 900
unloadTimeout: 30
globalTTL: 300
startPort: 5900
logToStdout: "proxy,upstream,http"
models:
  qwen-code:
    cmd: >-
      "C:/AI/runtimes/llama.cpp/bin/llama-server.exe" --model "C:/AI/models/Qwen3-Coder-30B-A3B/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf" --host 127.0.0.1 --port `${PORT} --ctx-size 32768 --threads 8 --flash-attn on --cache-type-k q8_0 --cache-type-v q8_0 --fit on --fit-target 768
    cmdStop: 'powershell.exe -NoProfile -NonInteractive -Command "Stop-Process -Id `${PID} -ErrorAction Stop"'
    ttl: 300
    checkEndpoint: /health
    capabilities:
      in: [text]
      out: [text]
      context: 32768
"@
}
$yamlNormalized = ($yamlPath -replace '\\', '/')
$pidNormalized  = ($pidPath -replace '\\', '/')
Set-Utf8NoBom $yamlPath $yamlContent

# Build safeqwen.json
if ($ThreeModel) {
    $cfgObj = @{
        executor                      = "safe_qwen"
        base_url                      = "http://127.0.0.1:9292"
        roles                         = @{
            code   = "qwen-code"
            critic = "nemotron-critic"
            review = "gptoss-review"
        }
        model_metadata                = @{
            "qwen-code"       = @{ display_name = "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M"; runtime = "llama.cpp" }
            "nemotron-critic" = @{ display_name = "Nemotron-3.5-Lightning-30B-A3B-Q4_K_M"; runtime = "llama.cpp" }
            "gptoss-review"   = @{ display_name = "GPT-OSS-120B-Q4_K_M"; runtime = "HotPin" }
        }
        startup_timeout_seconds       = 900
        shutdown_timeout_seconds      = 60
        request_timeout_seconds       = 300
        timeout_seconds               = 600
        verification_timeout_seconds  = 60
        min_available_ram_gb          = 44
        code_min_available_ram_gb     = 16
        min_active_available_ram_gb   = 6
        memory_recovery_timeout_seconds = 30
        max_actions                   = 32
        max_retries                   = 0
        max_file_bytes                = 65536
        max_output_chars              = 16000
        safe_qwen_gateway             = @{
            binary   = "C:/AI/repos/triglav-harness-public/runtime/llama-swap.exe"
            pid_file = $pidNormalized
        }
        safe_qwen_reviewer_policy     = @{
            type               = "hotpin"
            version            = 1
            min_available_ram_gb = 44
            max_working_set_gb = 32
            hot_experts        = "C:/AI/repos/hotpin-llm/hot-files/gpt-oss-120b-q4km/hot_top4.txt"
            hot_experts_sha256 = "f8575c4685e94f705683ea2d8491615aa0504e803bec4f1822687a82aeb30526"
        }
        critic                        = @{
            min_available_ram_gb   = 30
            request_timeout_seconds = 180
            max_input_chars        = 16000
        }
        reviewer                      = @{
            max_input_chars        = 20000
        }
        safe_qwen_runtime             = @{
            code   = @{
                binary         = "C:/AI/runtimes/llama.cpp/bin/llama-server.exe"
                model          = "C:/AI/models/Qwen3-Coder-30B-A3B/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf"
                gateway_config = $yamlNormalized
            }
            critic = @{
                binary         = "C:/AI/runtimes/llama.cpp/bin/llama-server.exe"
                model          = "C:/AI/models/Nemotron-3.5-Lightning-30B-A3B/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-Q4_K_M.gguf"
                gateway_config = $yamlNormalized
            }
            review = @{
                binary         = "C:/AI/runtimes/hotpin/llama.cpp/build/bin/Release/llama-server.exe"
                model          = "C:/AI/models/GPT-OSS-120B-Q4_K_M/gpt-oss-120b-Q4_K_M-00001-of-00002.gguf"
                gateway_config = $yamlNormalized
            }
        }
    }
} else {
    $cfgObj = @{
        executor                      = "safe_qwen"
        base_url                      = "http://127.0.0.1:9292"
        roles                         = @{
            code = "qwen-code"
        }
        model_metadata                = @{
            "qwen-code" = @{ display_name = "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M"; runtime = "llama.cpp" }
        }
        startup_timeout_seconds       = 900
        shutdown_timeout_seconds      = 60
        request_timeout_seconds       = 300
        timeout_seconds               = 600
        verification_timeout_seconds  = 60
        min_available_ram_gb          = 16
        code_min_available_ram_gb     = 16
        min_active_available_ram_gb   = 6
        memory_recovery_timeout_seconds = 30
        max_actions                   = 32
        max_retries                   = 0
        max_file_bytes                = 65536
        max_output_chars              = 16000
        safe_qwen_gateway             = @{
            binary   = "C:/AI/repos/triglav-harness-public/runtime/llama-swap.exe"
            pid_file = $pidNormalized
        }
        safe_qwen_runtime             = @{
            code = @{
                binary         = "C:/AI/runtimes/llama.cpp/bin/llama-server.exe"
                model          = "C:/AI/models/Qwen3-Coder-30B-A3B/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf"
                gateway_config = $yamlNormalized
            }
        }
    }
}
Set-Utf8NoBom $configPath ($cfgObj | ConvertTo-Json -Depth 6)

# Build CLI argument list
$allowArgs = @()
foreach ($a in $AllowPaths) { $allowArgs += @("--allow", $a) }
$forbidArgs = @()
foreach ($f in $ForbidPaths) { $forbidArgs += @("--forbid", $f) }

$cliArgs = @(
    "-B", $harnessPy,
    "--config", $configPath
)

if ($ThreeModel) {
    $cliArgs += "--critic"
}

$cliArgs += @(
    "autonomous-run",
    "--executor", "safe_qwen",
    "--safeqwen-policy", $policyPath,
    "--repo", $repoPath,
    "--task", $Task,
    "--verify", $Verify,
    "--acceptance", $Task
)
$cliArgs += $allowArgs
$cliArgs += $forbidArgs

if ($ThreeModel) {
    $cliArgs += @("--reviewer", "--review-policy", "three-model")
}

# DryRun branch with strict exit code propagation
if ($DryRun) {
    $cliArgs += "--dry-run"
    Write-Host "`n[DryRun] Validating configuration, scope, and budget without models..." -ForegroundColor Yellow
    Set-Content -Path $pidPath -Value "0"
    $pyExit = 0
    try {
        & python $cliArgs
        $pyExit = $LASTEXITCODE
    } finally {
        Remove-Item -Path $pidPath -Force -ErrorAction SilentlyContinue
    }
    if ($pyExit -ne 0) {
        Write-Error "[DryRun] Validation failed with exit code $pyExit."
        exit $pyExit
    }
    Write-Host "`n[DryRun] Complete. Configuration, scope, and budget valid. Zero models loaded, zero files modified." -ForegroundColor Green
    return
}

# Preflight: Check port 9292 is free before launching gateway
$portConn = Get-NetTCPConnection -LocalPort 9292 -ErrorAction SilentlyContinue
if ($portConn) {
    $ownerPids = ($portConn | Select-Object -ExpandProperty OwningProcess -Unique) -join ", "
    Write-Error "[Gateway] Port 127.0.0.1:9292 is already occupied by process PID(s): $ownerPids. Stop the existing gateway before launching Invoke-Triglav."
    exit 1
}

# Live execution: Start llama-swap
$swapBin = "C:\AI\repos\triglav-harness-public\runtime\llama-swap.exe"
Write-Host "`n[Gateway] Starting dedicated llama-swap gateway on 127.0.0.1:9292..." -ForegroundColor Cyan

$gwProc = Start-Process -FilePath $swapBin `
    -ArgumentList "-config `"$yamlPath`" -listen 127.0.0.1:9292" `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -PassThru -NoNewWindow

Set-Content -Path $pidPath -Value "$($gwProc.Id)"
Write-Host "[Gateway] Started PID $($gwProc.Id); PID file written."

$gwReady = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    if ($gwProc.HasExited) {
        $exitCode = $gwProc.ExitCode
        Remove-Item -Path $pidPath -Force -ErrorAction SilentlyContinue
        Write-Error "[Gateway] llama-swap process (PID $($gwProc.Id)) terminated prematurely with exit code $exitCode. See log: $stderrLog"
        exit 1
    }
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:9292/upstream" -TimeoutSec 2 -UseBasicParsing -ErrorAction SilentlyContinue
        if ($resp.StatusCode -eq 200) {
            $gwReady = $true
            break
        }
    } catch { }
}

if (-not $gwReady) {
    Write-Error "[Gateway] llama-swap did not become ready within 30s. Terminating gateway."
    try { Stop-Process -Id $gwProc.Id -Force -ErrorAction SilentlyContinue } catch { }
    Remove-Item -Path $pidPath -Force -ErrorAction SilentlyContinue
    exit 1
}
Write-Host "[Gateway] Gateway healthy and listening on 127.0.0.1:9292." -ForegroundColor Green

$pyExit = 0
try {
    Write-Host "`n[Execution] Starting TRIGLAV controller..." -ForegroundColor Cyan
    Write-Host "Command: python $($cliArgs -join ' ')" -ForegroundColor DarkGray
    & python $cliArgs
    $pyExit = $LASTEXITCODE
} finally {
    Write-Host "`n[Gateway] Stopping llama-swap gateway..." -ForegroundColor Cyan
    try {
        Invoke-WebRequest -Uri "http://127.0.0.1:9292/api/models/unload" -Method Post -TimeoutSec 3 -UseBasicParsing -ErrorAction SilentlyContinue
    } catch { }
    try {
        if (-not $gwProc.HasExited) {
            Stop-Process -Id $gwProc.Id -Force -ErrorAction SilentlyContinue
            $gwProc.WaitForExit(3000)
        }
    } catch { }
    Remove-Item -Path $pidPath -Force -ErrorAction SilentlyContinue
    Write-Host "[Gateway] Gateway stopped." -ForegroundColor Green
}

if ($pyExit -ne 0) {
    Write-Error "[Execution] TRIGLAV run failed or did not reach COMPLETED (controller exit code: $pyExit)."
    exit $pyExit
}

# Post-run results inspection
Write-Host "`n=== CANDIDATE DIFF ===" -ForegroundColor Yellow
$diff = git -C $repoPath diff
if ($diff) {
    Write-Host $diff
} else {
    Write-Host "(No files changed)"
}

Write-Host "`n=== INSTRUCTIONS FOR OPERATOR ===" -ForegroundColor Cyan
Write-Host "Target repository changes are strictly scoped to allowed files: $($AllowPaths -join ', ')"
$pathsQuoted = ($AllowPaths | ForEach-Object { "`"$_`"" }) -join " "
Write-Host "`n1. To ACCEPT and commit changes:" -ForegroundColor Green
Write-Host "   git -C `"$repoPath`" add $pathsQuoted"
Write-Host "   git -C `"$repoPath`" commit -m `"TRIGLAV: $Task`""
Write-Host "`n2. To REJECT and discard changes:" -ForegroundColor Yellow
Write-Host "   git -C `"$repoPath`" restore $pathsQuoted"
