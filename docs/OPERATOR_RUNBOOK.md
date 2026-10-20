# TRIGLAV operator runbook

The canonical operator is the tracked
[scripts/Invoke-Triglav.ps1](../scripts/Invoke-Triglav.ps1) in the release checkout.
The separately installed tools/triglav/Invoke-Triglav.ps1 differs: it lacks the
merged splash and -Quiet handling. Do not assume the copies are interchangeable.
This guide describes the tracked version at the [release baseline](TRIGLAV_START_HERE.md).

## Prerequisites and scope

Use native x64 Windows, Python 3.11+, Git and PowerShell. Run from the release
root; the controller Store must be outside the target repository. The target
needs a committed, clean Git baseline and trusted frozen tests. Verification
executes target code; Windows Job Objects supervise processes, not filesystem
or network isolation.

The wrapper embeds installation-specific runtime, model, expert-digest and
session paths. Inspect those values in the tracked script before use; -TriglavRoot
selects the controller checkout, not an alternative model/runtime installation.
Provisioned compatible llama-swap, llama-server and GGUF files are required even
for configuration validation. Use the [portable Windows recipe](WINDOWS_SETUP.md)
with local JSON/YAML configuration on a different installation.

Live single-model preflight requires 16 GiB available physical RAM; -ThreeModel
requires 44 GiB. The HotPin reviewer additionally requires the approved expert
identity and native 32 GiB hard working-set readback before inference. These
are policy gates, not guarantees about loading memory, total RAM or VRAM.
See [HotPin policy](HOTPIN_POLICY.md).

SafeQwen exposes only read_files, edit_file and submit. All -AllowPaths and
-ForbidPaths entries must already be regular files at baseline. Edits require
the current before_sha256 and one exact anchor occurrence. **No dynamic file
creation, deletion or model-selected shell commands are supported.** A clean
baseline does not promise automatic rollback of failed or ambiguous edits.

## DryRun first

After provisioning the installation and a disposable target, from the release root:

```powershell
$targetRepo = 'C:\projects\disposable-calculator' # example target, not the controller
$taskText = 'Implement multiply without changing add or the frozen tests'
$operatorArgs = @{
    Repo = $targetRepo
    Task = $taskText
    Verify = 'python -m unittest test_calculator.py'
    AllowPaths = @('calculator.py')
    ForbidPaths = @('test_calculator.py', '.gitignore')
    TriglavRoot = (Get-Location).Path
}
.\scripts\Invoke-Triglav.ps1 @operatorArgs -DryRun
```

DryRun checks the clean target, existing scope, generated configuration, command
allowlist and budgets, then delegates to the real controller's --dry-run.
It queries host RAM metadata but skips the wrapper's live RAM-floor checks;
controller resource/policy validation still applies. It starts no gateway or model
and runs no target verification commands.

**DryRun does write operator files:** a timestamped session directory containing
policy.json, safeqwen.json and llama-swap.yaml, plus a temporary gateway.pid with
value 0 that it attempts to remove in finally. The target files are not edited,
and the controller creates no durable run for dry-run. The script's “zero files
modified” success text must be understood with these session writes in mind.
Exit 0 means validation passed; it does not prove model readiness or task success.

Add -ThreeModel to validate the critic/reviewer configuration too. Add -Quiet to
suppress the splash and controller progress; operator diagnostics/results remain.

## Live startup and outcomes

Only after a passing dry-run and authorization for the intended local model run:

```powershell
# Single-model coding; omit -DryRun.
.\scripts\Invoke-Triglav.ps1 @operatorArgs
# Alternatively, choose the three-model route for a separate intended run:
# .\scripts\Invoke-Triglav.ps1 @operatorArgs -ThreeModel
```

Choose one invocation; do not run both automatically against the same fixture.
Each fresh run requires its own clean, reviewed target. Preserve failed attempts.

The live wrapper checks port 9292 for an existing connection, starts its dedicated
loopback llama-swap with stdout/stderr redirected into the session, writes its
PID file and waits up to 30 seconds for an HTTP response from /upstream. This is
gateway startup, not proof that a model is loaded. The controller then handles
model lifecycle, scoped execution and deterministic verification. The ordinary
entry point supplies one fixed WorkUnit with one worker attempt.

| Outcome | Meaning |
|---|---|
| DryRun exit 0 | Configuration/scope/budget validation only |
| Code-only exit 2, READY / MILESTONE_READY | Intermediate verified candidate; no trusted checkpoint. The wrapper propagates the nonzero controller outcome and may print a failure diagnostic |
| Three-model exit 0, COMPLETED with valid checkpoint evidence | Applicable critic/reviewer and fresh-final-verifier gates passed; inspect actual evidence |
| Other nonzero exit or uncertain cleanup | Preserve the candidate and evidence for review; no acceptance inference |

The three-model route runs Qwen execution, Nemotron advisory critique and GPT-OSS
review requiring APPROVE, followed by fresh deterministic verification and
controller checkpoint gates. Critic findings can be advisory; do not require a
CRITIC_CLEAN label as a universal acceptance condition.

**The ordinary wrapper/CLI does not enable terminal acceptance.** A checkpoint
is not an acceptance/term-*.json seal. Historical B2 completed with a checkpoint,
not terminal acceptance. The separate terminal lifecycle requires explicit
terminal_acceptance opt-in, verified cleanup, checkpoint commitment and WAL-bound
terminal sealing; see [architecture](../ARCHITECTURE.md).

## Inspect and stop

The controller prints the run ID and stores evidence under its own `runs/<run-id>/`:
events.jsonl is authoritative, state.json is a cache, final-report.json is a
projection; milestones/, final_verification/ and checkpoints/ hold referenced
artifacts. Terminal-enabled runs additionally have acceptance/. Gateway logs and
generated configuration remain in the separate operator session directory.
Keep these private; preserve both successful and failed evidence.

```powershell
git -C $targetRepo diff
# If independent target verification is authorized, run it in the target:
Push-Location -LiteralPath $targetRepo
try { python -m unittest test_calculator.py } finally { Pop-Location }
```

The operator reviews and authorizes any later commit or discard. The wrapper
leaves candidate edits uncommitted; it does not grant permission to publish them.

On normal return or a catchable interruption, finally attempts model unload,
gateway stop and temporary PID-file removal. Some failures are suppressed;
“Gateway stopped” is not independent proof that every owned process exited.
Ctrl+C is not qualified safe live interruption/resume. Inspect ownership and
retained evidence before further action; never terminate an unknown process or
assume a failed run is safe to resume. See [supervision](../PROCESS_SUPERVISION.md)
and [unqualified scope](TRIGLAV_START_HERE.md).
