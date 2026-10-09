# Process supervision (Phase 2A)

This phase hardens one bounded durable workflow. It adds no Manager, autonomous
rounds, shell authority, or model policy changes. Qwen remains executor/repairer,
Nemotron advisory, GPT-OSS senior arbitrator, and deterministic gates plus durable
checkpoints remain authoritative.

## Ownership and containment

`supervision.py` commits launch intent before starting important child work. Records
contain run/child IDs, purpose, controller PID, executable, safe command SHA-256,
random nonce, boot GUID, PID/creation identity, timestamps, heartbeat, state,
termination kind and known exit code. PID alone never proves ownership. Observation
compares boot, creation time and executable; forced cleanup additionally proves job
membership using retained native handles, avoiding a check-PID/kill-PID reuse race.
There is no durable process-name killing.

`supervision_worker.py` joins a nonce-named Windows Job Object with
`KILL_ON_JOB_CLOSE` before starting the allowlisted verifier. Descendants inherit
the job; breakaway is disabled. The controller opens a nonce-matching gate only
after fsyncing broker identity. Without a gate the broker exits after 15 seconds
without executing work. Failed job assignment has no uncontained fallback.
The broker represents the verification group; descendants are contained group
members, not individually adopted by name or parent-PID enumeration.

The broker holds the long-lived job handle. Controller death can leave bounded
work observable; fresh resume refuses duplicate execution. Wait for exit and rerun
the gates. Broker death closes the job and kills descendants. Terminal closure or
an enclosing job may also end the broker safely. Jobs cannot survive reboot.
See Microsoft's [Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects).

A receipt distinguishes natural command exit, forced timeout, and launch failure.
Job closure always cleans lingering descendants; the controller confirms the job
is empty before accepting completion. Forced owner cleanup commits STOPPING,
rechecks boot/identity/membership on native handles, terminates that job, waits for
emptiness, then records EXITED/forced. Recovered receipts alone never authorize
verification or a checkpoint. A crash during cleanup remains STOPPING or LOST.

## Delegated models

llama-swap stays externally managed. Durable startup records intent, rendered
executable/config fingerprint, role/alias/profile and exact gateway parent identity.
RUNNING requires one native model with the expected executable and verified parent.
Existing RAM, HotPin, zero-server and one-large-model-at-a-time guards remain.
Durable unload requires the current run's verified model/gateway and unambiguous
gateway listing; it records STOPPING and forced shutdown because the configured
stop command terminates the child. The API owns shutdown; the harness never
PID-kills models. Resume refuses live delegated models. Ambiguous startup remains
UNKNOWN and requires inspection or reboot. An empty gateway (`/running` and native
server list both empty) is never proof that an earlier ambiguous launch resolved:
any STARTING/UNKNOWN model child blocks the next model registration, unload
and launch, even when a different model is running, and blocks resume.

### Intentional stop vs. unexpected disappearance (Phase 2A.2)

A delegated model that the harness itself asked to stop (`STOPPING`, committed by
the unload guard) is expected to vanish. The live controller's periodic heartbeat
therefore leaves such a model `STOPPING` (reason `stop_requested_exit_seen`) when
its PID disappears, instead of committing `LOST/exit_unobserved`. Only
`models_unloaded()` finalizes it, as `EXITED/forced`, and only after `/running` and
the native server list were verified empty and the PID is confirmed gone; a PID
disappearance alone never records `EXITED`. Everything else is unchanged: an
unexpected disappearance from `RUNNING` is still `LOST`; identity mismatch is still
`STALE/PID_REUSED`; reboot is still `LOST_AFTER_REBOOT`; non-model children are not
exempt; and resume/status/`require_idle` still turn an orphaned `STOPPING` record
(controller died mid-unload) into `LOST`, so it cannot block forever. Real GPT-OSS
exposed this race (the heartbeat fired between `process_stopping` and unload
completion); the record, not safety, was wrong.

### Explicit operator recovery of an UNKNOWN model child (Phase 2A.1)

Automatic resume, `Gateway.switch` and unload **never** clear UNKNOWN: a missing or
unverifiable identity cannot be distinguished from a model that really started,
and an empty gateway is not proof. Before this phase the only exit was a reboot.
An operator who has inspected the machine can now resolve exactly one record:

```powershell
python harness.py recover-model --run-id <run> --child-id <model_server-id> `
    --resolution model_not_running --reason "Inspected gateway and Task Manager; no llama-server" --confirm
python harness.py resume --run-id <run>     # separate, normal, fully gated step
```

The command holds the durable controller lock, loads/validates the run, and refuses
unless **all** hold: `--confirm` given; run ID matches; the child is recorded in
that run, is a `model_server`, is `UNKNOWN` after a fresh reconciliation (RUNNING,
STOPPING, EXITED, LOST, STALE/PID_REUSED, already-RESOLVED and non-model children
are refused) with an eligible reason (`launch_identity_unproven` or
`delegated_launch_ambiguous`, not e.g. `inspection_denied`); the reason is 1-200
printable characters; and the dedicated gateway is reachable and reports **no
loaded model while the native server list is also empty**. Any live model/server
refuses, because it cannot be attributed to the record; PID equality is never used
as authority and nothing is ever signalled.

On success the child moves to the terminal state `RESOLVED` (validated, versioned
`resolution` = version, resolution, reason, time, prior state/reason, ledger
revision). History is append-only: the earlier STARTING/UNKNOWN commits remain in
`events.jsonl`, and the resolution is one WAL commit whose transition is
`operator_model_resolution` with the resolution, reason and prior state. No
username or host identifier is recorded. It changes no checkpoint, verified
progress, unverified work, run status or other child; other UNKNOWN children stay
blocking. Repeating the command is refused (`already operator-resolved`) without
new history. Resolution is an attestation-plus-check, not proof that the original
launch never started; a model that started and is still loaded blocks it, and the
next launch still passes every normal RAM, zero-server and unload guard.

The gateway must be dedicated and exclusively operated. Its API has no transactional
PID/nonce-scoped unload: checks cannot protect against an external caller replacing
models between inspection and the API request. Model children are not members of
verifier jobs; supervision does not claim arbitrary gateway processes as owned.

## Heartbeats, reboot and WAL

Controller heartbeats and active-child observations occur every 15 seconds. Age
over 45 seconds, backward clock movement or boot change means stale. Fresh means
recently observed, not exclusive ownership or VERIFIED progress. The kernel lock
excludes competing durable controllers. A reentrant store lock serializes heartbeat
and foreground writes. Heartbeat persistence failure blocks checkpoint acceptance.

Structured registration/start/heartbeat/exit/mismatch/loss/reboot/drift/reconciliation
transitions are `state_committed.transition` metadata alongside state in one
hash-chained, fsynced WAL record. Atomic cache replacement and torn-tail replay
remain unchanged. No hidden reasoning, prompts or raw model replies are added;
bounded ledger output and broker spools are verifier evidence. No one-second flood.

Boot identity is the Windows boot GUID from dynamically loaded
`NtQuerySystemInformation(SystemBootEnvironmentInformation)`. No hardware serial,
device UUID or username is collected. Clock, boot and process inspection are
injectable. The native API is subject to OS changes; unavailable/invalid information
fails closed without PID or estimated-boot fallback. See Microsoft's
[API caveat](https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntquerysysteminformation).
Changed boot makes old live records LOST/LOST_AFTER_REBOOT before PID inspection or
signaling. Repository/checkpoint matching remains mandatory.

## Environment fingerprint v1

Runs and checkpoints bind deterministic fingerprints of OS version/build/architecture;
Python version/executable/DLLs/flags; harness Git HEAD and execution-source SHA-256;
pinned config, commands and review profile; source config, swap template/rendered
YAML/profile and expert-selection files; runtime executables/adjacent DLLs; resolved
verifier/Git executables; model paths, each discovered shard's size/mtime and
configured model metadata/known hashes. No device identifiers are included.

Critical files up to 256 MiB are strongly hashed. Missing, unreadable, oversized or
changing critical files fail closed. Model weights are never rehashed on resume;
configured hashes are retained evidence, not newly verified claims. Model metadata
cannot detect hostile replacement preserving exact size/mtime. The controlled swap
template parser requires established explicit quoted paths; unsupported formats
fail closed rather than executing YAML/interpolated commands.

| Class | Resume behavior |
| --- | --- |
| MATCH | Normal repository/process recovery |
| INFORMATIONAL | Only mtime of a strongly hashed file changed; continue |
| REVALIDATION_REQUIRED | Model metadata, compatible profile/config artifact, OS or Python patch changed; require `--revalidate-environment`, then rerun all gates without implementation/repair |
| UNSAFE | Changed runtime/source, harness HEAD, repository, commands, incompatible effective config/flags, source harness config or Python major/minor; unknown runtime/model identity; refuse |

Resume keeps pinned config. Source harness-config changes conservatively require
restoration or a new run, even when potentially compatible. Revalidation sets a
durable pending flag: another crash cannot reconstruct the old checkpoint as
current in the changed environment. Prior checkpoints remain historical trusted
evidence. Meaningful drift during execution also blocks checkpoint creation.
Environment acknowledgment never overrides unsafe drift or unfinished-edit consent.

## Resume decisions and crash windows

Resume validates schema/WAL and repository/checkpoint, reconciles boot/children,
then compares environment, before any model access.

| Evidence/window | Result |
| --- | --- |
| Exact checkpoint, idle children, matching environment | Reconstruct without models |
| Exact initial baseline | Restart bounded workflow |
| Exactly recorded unfinished edits | Require `--revalidate-unverified`; rerun gates |
| Child starts before identity persists | Intent exists; gate closed; UNKNOWN refuses resume |
| Intent persists but spawn fails | EXITED/launch_failed if observed, otherwise UNKNOWN |
| Controller dies immediately after startup/during verifier | Recorded broker/job remains observable; live child blocks duplicate |
| Controller dies during model | Live delegated child blocks resume; ambiguous launch UNKNOWN |
| Child exits while controller down | LOST, exit code unknown, never fabricated success |
| PID reused | STALE/PID_REUSED; unrelated PID never signaled |
| Reboot | LOST_AFTER_REBOOT before PID lookup; recover from repository anchor |
| Denied inspection/ambiguous identity | UNKNOWN; never signal; refuse (operator `recover-model` only when eligible and the gateway/servers are verified empty) |
| Environment changes | Refuse or demand explicit new gates by drift class |
| Heartbeat/state write tears | WAL replay/torn-tail recovery; no progress promotion |
| Controller dies during cleanup | STOPPING survives; observe or refuse |

`status --run-id ID` gives a concise read-only summary without models. `--json`
includes full state, historical children and before/after drift. Phase 1 schema-v1
records remain readable; those without the versioned supervision/environment
extension refuse automatic continuation because historical identities cannot be
safely invented.

## Limits and Phase 2B

Durable brokers require Windows nested Jobs (Windows 8+). Ordinary `run` retains
its previous execution path. Native API failure, incompatible enclosing jobs or
Local job namespace/session permissions can prevent cleanup. Timeouts bound
verifier runtime; raw spools may grow on disk until timeout, while ledger output
is bounded. Short synchronous read-only Git probes are bounded helpers, not
independently resumable work. Tests and local evidence storage remain trusted;
checksums detect corruption, not hostile rewrites. No public force-resume or
kill-by-PID override is provided.

Phase 2B should add a bounded Manager choosing one step and acceptance contract at
a time, with total time/round/retry budgets, a stall policy, fresh executor contexts
and independent gates per checkpoint. Consume reconciliation decisions without
weakening ownership, drift policy, role authority or model concurrency limits.
