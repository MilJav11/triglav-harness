# TRIGLAV operator runbook

The canonical operator is the tracked
[scripts/Invoke-Triglav.ps1](../scripts/Invoke-Triglav.ps1) in the release checkout.
The separately installed tools/triglav/Invoke-Triglav.ps1 differs: it lacks the
merged splash and -Quiet handling. Do not assume the copies are interchangeable.
This guide describes the tracked version at the [release baseline](TRIGLAV_START_HERE.md).

## Interactive launcher

From the release root in Windows PowerShell 5.1 on Windows 11:

```powershell
powershell.exe -NoProfile -File .\scripts\Start-Triglav.ps1
```

The menu supports new tasks, loading a project profile, inspecting previous runs,
managing profiles and help/diagnostics. Choose **Start new coding task**, enter
the target Git root (spaces and Unicode are supported), and select Qwen-only
(default) or three-model mode. Enter the task, existing editable files and
explicit frozen files, then the deterministic verification command. Each section
has a numbered heading. Enter one existing relative path at each **EDITABLE FILE**
or **FORBIDDEN FILE** prompt. **Press ENTER on an empty line to finish.** Missing
or invalid files, duplicates and scope overlap are rejected immediately; correct
the entry in the same step without losing your project, model, task or earlier
valid files. The pre-DryRun summary lists editable and forbidden files separately.
Both lists need at least
one file for this operator. SafeQwen cannot create new files. Type `back` at any
prompt to return to the menu; file-entry errors stay in the current step.
The flowing menu tolerates resizing without cursor positioning. The canonical
operator retains its own adaptive splash.

Review the configuration preview. Three-model mode requires typing `THREE` for
each configuration and can need substantially more RAM, time and loading. It
does not promise a checkpoint or terminal acceptance. Press Enter to run the
mandatory DryRun. It delegates to the canonical operator, starts no model,
executes no target verification and writes operator session configuration as
described below. A failed DryRun blocks live execution. Following a passing
DryRun, the launcher rechecks repository identity and cleanliness, shows a safety
summary, and requires typing the exact word `RUN` for one live run. Enter cancels.
It checks again after confirmation. Any changed configuration requires a fresh
DryRun; profiles never bypass this sequence. There is no automatic retry,
stash, reset, commit, push or rollback.

Profiles use version-1 JSON under the current user's local application data
directory, `TRIGLAV/Launcher/profiles/`, outside the project. Saving is optional;
task descriptions are excluded unless explicitly selected. Keep secrets out of
all profile fields, including commands and optional task text. Unknown fields,
unsupported versions, invalid scope and corrupted JSON are rejected. Writes use
an atomic same-directory rename without overwriting existing profiles. Manage
profiles to inspect or explicitly delete one; use a new name to save changed
preferences. The selector shows at most 30 profiles. The launcher keeps task
invocation data in memory, although the canonical operator/controller persist
their normal session and task evidence.

After a run, inspect the operator exit and execution outcome separately from
persisted trust artifacts. A failed execution remains **FAILED /
HUMAN_ACTION_REQUIRED** even when a valid earlier checkpoint survives. Unexpected
operator-exit/WAL disagreements are shown explicitly and require inspection.

- **VERIFIED INTERMEDIATE CANDIDATE**: `READY` with reason `MILESTONE_READY`,
  normally exit **2** for Qwen-only. This is not generic model failure and does
  not guarantee a trusted checkpoint.
- **Retained trusted checkpoint**: the persisted checkpoint and its referenced
  evidence pass the existing controller validators. This is reported separately
  and does not establish current execution success.
- **Terminal acceptance evidence: VALIDATED**: only a validated WAL-bound
  acceptance transaction and seal permit this label. The ordinary canonical operator does not enable
  this gate. Historical terminal-enabled runs can be inspected.
- **FAILED / HUMAN_ACTION_REQUIRED**, or **UNKNOWN** when evidence cannot be
  validated or correlated: preserve changes and inspect the diagnostic and
  evidence location. An exit of 0 alone never establishes trust.

Current invocation evidence is selected only by the unique `Autonomous run:` ID
printed by that operator process and matched against the target, task, scope,
mode and verifier. The launcher never guesses from timestamps or another recent
run. It reports recorded verification, changed paths, controller cleanup and
trust where available. Gateway process cleanup remains **UNKNOWN** because the
wrapper's diagnostics do not independently prove all owned processes exited.
The bounded previous-run menu rejects linked/reparse-point run directories and
linked runs roots. It shows the latest 20 eligible persisted run IDs; selecting
one reads the WAL through existing validators without regenerating historical
reports or modifying evidence bytes. Missing, malformed and stale report
projections are identified; validated WAL state remains authoritative. This
inspection can flush an existing terminal WAL through the controller's loader;
it does not repair it. Incomplete WAL tails or orphan seals require manual review.

To install a desktop shortcut **only after reviewing the implementation**, run:

```powershell
powershell.exe -NoProfile -File .\scripts\Start-Triglav.ps1 -InstallShortcut
```

This explicit command uses the actual Windows Desktop folder (including a
redirected/OneDrive Desktop), opens PowerShell with `-NoExit`, quotes the resolved
launcher path, uses the built-in PowerShell icon and refuses an existing
`TRIGLAV.lnk`. It requires no administrator access, global PATH change,
execution-policy bypass, service or autostart entry. If local policy blocks a
script, follow your organization's policy; the launcher does not change it.
Moving the checkout requires recreating the shortcut explicitly.

If the launcher is unavailable, use `scripts/Invoke-Triglav.ps1` directly with
the splatted arguments in **DryRun first** below. The launcher does not make
installation-specific paths portable, add terminal acceptance to that operator,
or qualify live cancellation/resume. Ctrl+C during inference still requires
manual evidence/process inspection; it is not a rollback facility.

Focused offline validation (fake model runs, no shortcut installation):

```powershell
python -B -m unittest discover -s tests -p "test_launcher.py" -v
python -B scripts/ci_supplemental.py check
```

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
