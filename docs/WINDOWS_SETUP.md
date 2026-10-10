# Portable Windows setup and local coding demo

The source/offline path requires native Windows, x64 Python 3.11+, Git and
PowerShell. Python 3.11 is the guarded CI baseline. No pip install or model is
needed for `python -B scripts/ci_offline.py` (209 reviewed cases, model calls 0).
Use an actual Git checkout: the controller records its real HEAD. Offline tests
use disposable Git/Python subprocesses and Windows Job Objects; a shell denying
those operations cannot run them. Do not relax the guards.

Public Run 003 qualified one operator-provisioned three-model deployment; see
[its sanitized summary](triglav/QUALIFICATION.md#public-run-003). A new clone still
needs licensed native runtime/model assets for live use. It does not reproduce
historical TQ-02R or distribute its private evidence. No private watchdog,
qualification broker, telemetry service or Stage 2 helper is required by this
public workflow. The included `supervision_worker.py` remains the existing
verifier supervision broker, not a new service.

## External assets

Separately obtain compatible binaries, matching DLLs/backends and licensed model
files. These existing references are acquisition links, not an automatic
installer or a promise that the newest runtime accepts every retained flag.
Record the runtime revision and hashes; inspect that binary's `--help` against
its configured arguments before model startup. Keep assets in ignored `runtime/`
or an external local directory. No asset is fetched by these examples.

| Component | Public source / acquisition | Required local asset |
|---|---|---|
| Python | [Python Windows releases](https://www.python.org/downloads/windows/) | Native x64 Python 3.11+ and standard library |
| Git | [Git for Windows](https://git-scm.com/install/windows) | `git` available to PowerShell and Python |
| llama-swap | [v260 release](https://github.com/mostlygeek/llama-swap/releases/tag/v260), the retained version | Windows executable, dedicated loopback gateway, operator-owned PID record |
| llama.cpp | [Upstream source and releases](https://github.com/ggml-org/llama.cpp) | Compatible Windows `llama-server.exe` plus its matching DLLs/backend files for Qwen and Nemotron |
| GPT-OSS HotPin runtime | [HotPin upstream](https://github.com/LozzKappa/hotpin-llm) | Separate patched llama.cpp server and matching DLLs; model-compatible Top4 expert selection file |
| Qwen code model | [Published Qwen GGUFs](https://huggingface.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF) | `Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf` |
| Nemotron critic model | [Published Nemotron GGUFs](https://huggingface.co/aj9o9/nvidia-nemotron-3.5-lightning-30b) | `NVIDIA-Nemotron-3.5-Lightning-30B-A3B-Q4_K_M.gguf` |
| GPT-OSS reviewer model | [Published GPT-OSS GGUFs](https://huggingface.co/unsloth/gpt-oss-120b-GGUF) | `gpt-oss-120b-Q4_K_M-00001-of-00002.gguf` **and** `gpt-oss-120b-Q4_K_M-00002-of-00002.gguf`, together |


Code-only needs only Qwen, a compatible standard llama-server and llama-swap.
Three-model mode additionally needs Nemotron, every GPT-OSS model shard and a
compatible separate HotPin server with an operator-approved expert selection.
The actual expert path/hash must be supplied; no expert asset is bundled and no
full-layer-coverage rule is imposed by the public policy. See
[policy semantics](HOTPIN_POLICY.md). Runtime capability and available host
resources require independent validation on each new deployment.

## Local Qwen demo

Use a fresh disposable checkout and a separate target, never the controller or
an existing successful fixture. First follow the exact
[calculator creation commands](../examples/calculator/README.md). Expect four
addition tests passing and six missing-multiply errors, then a clean target Git
status. Record the frozen test SHA-256. The baseline commit is in the disposable
target only; it is not a controller checkpoint.

### Configure only the code role

From the controller root, with no existing operator deployment to overwrite:

```powershell
$localFiles = @('config/safeqwen.local.json', 'config/safeqwen-policy.local.json', 'runtime/llama-swap.yaml')
foreach ($file in $localFiles) {
  if (Test-Path -LiteralPath $file) { throw "Preserve existing deployment: $file" }
}
New-Item -ItemType Directory -Force runtime | Out-Null
Copy-Item config/safeqwen.example.json config/safeqwen.local.json
Copy-Item config/safeqwen-policy.example.json config/safeqwen-policy.local.json
Copy-Item config/llama-swap.safeqwen-code.example.yaml runtime/llama-swap.yaml
$runtime = (Resolve-Path runtime).Path.Replace('\', '/')
$yaml = Get-Content -LiteralPath runtime/llama-swap.yaml -Raw
[IO.File]::WriteAllText((Join-Path $runtime 'llama-swap.yaml'), $yaml.Replace('C:/TRIGLAV-runtime', $runtime), [Text.UTF8Encoding]::new($false))
```

JSON paths resolve against the operator JSON's `config/` directory. Its defaults
expect `runtime/llama-swap.exe`, `runtime/llama-server.exe` (with matching DLLs)
and `runtime/qwen-model.gguf`. Point JSON and YAML at your provisioned files if
they live elsewhere. The names are configurable, not runtime discovery. Every
YAML executable/model path must be absolute and identify the SAME files as JSON.
Keep each alias once, two-space model indentation, `cmd: >-`, and the entire
quoted executable/quoted `--model` command on one following line; the existing
binding parser deliberately accepts only this controlled shape.

The code-only example enables exactly `code`, no critic, reviewer or expert
asset. It retains the qualified Qwen command settings and bounded 32-operation,
600-second worker contract. Its 16 GiB free-RAM startup example is not a universal
hardware guarantee; validate the chosen server/model on your host. Do not reduce
safety gates to force a startup.

### Start a dedicated, empty Gateway

Do not use the legacy `scripts/start-swap.ps1` for SafeQwen; it renders the custom
executor's profile templates. Start the dedicated Gateway below in the SAME
PowerShell session used for shutdown. Verify compatible llama-swap CLI first;
the retained deployment used v260. This block starts a Gateway, **not a model**.
It must not be run as part of source-only/offline CI.

```powershell
$gatewayExe = (Resolve-Path runtime/llama-swap.exe).Path
$gatewayYaml = (Resolve-Path runtime/llama-swap.yaml).Path
$pidFile = Join-Path (Resolve-Path runtime).Path 'llama-swap.pid'
if (Test-Path -LiteralPath $pidFile) { throw 'Existing PID record: inspect ownership first' }
if (@(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^llama-(server|swap).*\.exe$' }).Count) { throw 'Existing native model/Gateway process' }
if (@(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in @(9292,5900,5901,5902) }).Count) { throw 'Required port already owned' }
& $gatewayExe -config $gatewayYaml -validate
if ($LASTEXITCODE -ne 0) { throw 'Invalid Gateway configuration' }
$gateway = Start-Process -FilePath $gatewayExe -ArgumentList '-config', ('"' + $gatewayYaml + '"'), '-listen', '127.0.0.1:9292', '-watch-config=false' -WindowStyle Hidden -PassThru -RedirectStandardOutput runtime/gateway.stdout.log -RedirectStandardError runtime/gateway.stderr.log
$heldGatewayHandle = $gateway.Handle
$gatewayCreated = $gateway.StartTime.ToUniversalTime()
Start-Sleep -Seconds 2
$gateway.Refresh()
if ($gateway.HasExited -or $gateway.Path -ne $gatewayExe) { throw 'Gateway identity/startup failed; inspect owned process' }
$listener = @(Get-NetTCPConnection -State Listen -LocalPort 9292)
if ($listener.Count -ne 1 -or $listener[0].LocalAddress -ne '127.0.0.1' -or $listener[0].OwningProcess -ne $gateway.Id) { throw 'Unexpected listener ownership' }
Invoke-RestMethod 'http://127.0.0.1:9292/health' -TimeoutSec 5
if (@((Invoke-RestMethod 'http://127.0.0.1:9292/running' -TimeoutSec 5).running).Count) { throw 'Unexpected model loaded' }
if (@(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^llama-server.*\.exe$' }).Count) { throw 'Unexpected model process' }
[IO.File]::WriteAllText($pidFile, ([string]$gateway.Id + "`n"), [Text.Encoding]::ASCII)
```

No preload/hooks/automatic loading are configured. `/health` and `/running` are
non-loading checks; `/upstream/<alias>/health` **loads that model** and must not
be used for Gateway-only preflight. Never fabricate a PID or borrow another
running service. If startup verification fails, preserve evidence and inspect
only the process object you started; do not blindly kill by PID or delete a
record before confirmed termination.

### Dry-run, one live coding attempt and verification

Run the [README command](../README.md#local-qwen-calculator-demo) with `--dry-run`.
Require exit 0 and models_contacted=false. Dry-run validates configuration, file
scope and identities; it does not prove native model loading, process ownership,
working-set enforcement or actual inference. On failure, stop and preserve it.

Only after successful preflight and operator authorization, remove `--dry-run`
and execute once. The code-only result is expected to be exit 2,
READY / MILESTONE_READY, not a trusted checkpoint. After it stops:

```powershell
git -C ../disposable-calculator diff --name-only
git -C ../disposable-calculator diff -- calculator.py
Get-FileHash ../disposable-calculator/test_calculator.py -Algorithm SHA256
Push-Location ../disposable-calculator
python -B -m unittest -v test_calculator.py
Pop-Location
```

Require only calculator.py changed, the original test hash and 10/10 passing
tests. Inspect the recorded run with `python -B harness.py status --run-id
<run-id> --json`. Do not reset the target, manufacture a checkpoint or silently
rerun a failed attempt. Code-only success proves coding/verification, not
independent review or long-horizon recovery.

### Ownership-safe shutdown

The controller owns model-child unload/verification; the operator owns the
Gateway. Retain `$gateway` and its original handle from startup. After the
controller exits, confirm its cleanup evidence and perform only non-loading
checks. If any model remains or ownership is uncertain, STOP and preserve the
Gateway/PID record for inspection; do not use taskkill or another service's PID.

```powershell
$gateway.Refresh()
if ($gateway.HasExited -or $gateway.Path -ne $gatewayExe -or $gateway.StartTime.ToUniversalTime() -ne $gatewayCreated) { throw 'Gateway identity changed or already exited; inspect record' }
if (@((Invoke-RestMethod 'http://127.0.0.1:9292/running' -TimeoutSec 5).running).Count) { throw 'Models still loaded; preserve ownership evidence' }
if (@(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^llama-server.*\.exe$' }).Count) { throw 'Native model still present' }
if ((Get-Content -LiteralPath $pidFile -Raw).Trim() -ne [string]$gateway.Id) { throw 'PID record changed' }
$gateway.Kill()
if (-not $gateway.WaitForExit(30000)) { throw 'Termination unconfirmed; retain PID file' }
Remove-Item -LiteralPath $pidFile
$gateway.Dispose()
```

Protect configurations/runtime files from concurrent modification. These
application ownership safeguards do not isolate hostile code or other host
actors. Store raw evidence only under ignored runtime/runs or outside the checkout.

## Optional three-model workflow

Stop the code-only Gateway first; use a NEW clean calculator fixture for each
qualification. Do not reuse the edited code-only target. Copy the existing
`safeqwen-three-model.example.json`, policy and
`llama-swap.safeqwen.example.yaml` into the same ignored deployment locations,
only after preserving/removing your prior deployment deliberately. Replace every
fictional C:/TRIGLAV-runtime path and align JSON/YAML resource identities.

The three-role JSON now includes the complete required v1 policy, with a
**deliberately invalid digest placeholder**. Supply an operator-approved local
expert asset and trusted expected digest before any dry-run/startup. A current
hash identifies bytes; it does not establish provenance or operator approval.
Do not copy another host's approval or generate/replace an expert file implicitly.
All GPT-OSS shards and compatible HotPin DLLs are external prerequisites.

Use the same dedicated Gateway/PID checks, with no preload. Require known
available RAM >=44 GiB before review startup. The reviewed native gate must
verify the exact Supervisor child/executable/creation/parent identity, inherited
expert path/bytes and effective 32 GiB HARD working-set readback BEFORE review
inference. Cached ready reuse repeats the gate; TTL expiry returns to full
unload/startup/ownership checks. Legacy review_profiles remain rejected here.
The cap is process working set after readiness, not total RAM/VRAM or loading
memory. Never bypass a failed native gate or fall back to an unsafe profile.

```powershell
python -B harness.py --config config/safeqwen.local.json --critic autonomous-run `
  --executor safe_qwen --safeqwen-policy config/safeqwen-policy.local.json `
  --repo ../disposable-calculator --task "Implement multiply in calculator.py" `
  --verify "python -m unittest test_calculator.py" `
  --acceptance "All ten frozen tests pass; only calculator.py changes" `
  --allow calculator.py --forbid test_calculator.py `
  --reviewer --review-policy three-model --dry-run --quiet
```

Require dry-run exit 0. Removing only --dry-run under explicit operator
authorization invokes all three models. Actual PASS requires critic evidence,
independent reviewer APPROVE, fresh final tests, bound trusted checkpoint and
verified cleanup, not merely ten passing tests. Use the same shutdown rules.
[Run 003](triglav/QUALIFICATION.md#public-run-003) demonstrated this on one host;
new deployments remain independently unqualified until their own controlled test.

## Compatibility and publication

Legacy custom-executor templates remain available with fictional runtime paths;
edit them before using legacy start-swap.ps1. Their profile names/flags are
unchanged. The optional Cline fallback now uses ignored runtime/cline/cline.exe;
explicit cline.executable and PATH lookup remain supported. SafeQwen needs neither
Cline nor OpenCode. Source changes intentionally invalidate previous source pins;
never repin or modify historical Stores to force resume.

Public source includes the fixture, examples, AGENTS.md and all reviewed tests.
It excludes runtimes, weights, expert assets, PID files, local configs, raw Stores,
private evidence and secrets. See [CI scope](CI.md) and [licensing](../LICENSING.md).
