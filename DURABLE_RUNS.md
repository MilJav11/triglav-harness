# Durable runs and recovery (Phases 1 and 2A)

Phase 2A supersedes the Phase 1 process/runtime limitations described below.
[PROCESS_SUPERVISION.md](PROCESS_SUPERVISION.md) defines ownership, Job Object
containment, heartbeat/reboot semantics, environment fields, drift policy and
the resume/crash decision matrix. Live or ambiguous children now block resume
before any model unload/startup. Runtime/config/model environment evidence is
bound to new runs and checkpoints. Phase 1 records remain readable but lack the
historical evidence required for automatic continuation under Phase 2A.

`status --run-id ID` now prints a concise supervision summary; add `--json` for
full machine-readable state. Compatible environment drift requires
`resume --run-id ID --revalidate-environment` to rerun all gates without
implementation. Unsafe drift is never overridden. Unfinished edits still require
`--revalidate-unverified` as well. Historical trusted checkpoints remain intact
if later process work or environment revalidation fails.

`run` retains its workflow and result format. `long-run` opts into persistence
around **one bounded workflow**, including its existing repair budget. Manual
recovery attempts get new round directories and fresh model context. There is
no autonomous multi-round planner, automatic Git commit, or merge authority.

## CLI and recovery

```powershell
python harness.py long-run --repo .\target-repo --task "Fix the boundary case" --verify "python -m unittest discover" --acceptance "The boundary value is accepted"
python harness.py --critic long-run --repo .\target-repo --task "Fix the boundary case" --verify "python -m unittest discover" --reviewer
python harness.py status --run-id <printed-id>
python harness.py resume --run-id <printed-id>
python harness.py resume --run-id <printed-id> --revalidate-unverified
python harness.py recover-model --run-id <printed-id> --child-id <model_server-id> --resolution model_not_running --reason "<why>" --confirm
```

`recover-model` is the only way to clear an ambiguous (UNKNOWN) delegated model
record other than a reboot. It is explicit, audited, append-only, never run by
`resume`, never signals a process and never alters checkpoints or unverified work;
see [PROCESS_SUPERVISION.md](PROCESS_SUPERVISION.md) for its refusal conditions.

`--verify` and `--acceptance` are repeatable; `--quiet` is supported. Resume uses
the saved task, criteria, commands, configuration, role identities, review profile
and critic/reviewer choices. It cannot silently substitute current configuration.
Status contacts no inference servers and works during an active writer. Missing
environment components are reported as unsafe/unknown. A recorded phase does not
imply its old process is alive.

* Exact accepted checkpoint: reconstruct trusted progress/final report without
  models, edits or a new round. Reviewed dirty files are allowed only if exact.
* Unchanged initial baseline: restart the bounded workflow with fresh context.
* Exactly recorded unfinished edits: require `--revalidate-unverified` to rerun
  all gates, without implementation/repair. The flag never approves work or
  bypasses repository matching.

Unexpected changes, missing/moved/replaced repositories, changed HEAD/branch or
index, corrupt evidence/state, and unknown schema versions fail closed. Recovery
never resets/stashes/overwrites target files. Preserve the checkout and evidence,
inspect the diagnostic, and externally restore the exact baseline/checkpoint
before retrying. A kill between a file replacement and its durable observation
intentionally requires manual recovery. There is no force flag.

## Storage and trust

```text
runs/<run-id>/
  original-task.txt
  state.json
  events.jsonl
  rounds/0001/{verifier-*.json,reviewer-*.json,critic-*.json,failure.json,...}
  rounds/0002/...
  checkpoints/0001.json
  final-report.json
  torn-event-*.bin     # preserved debris from an interrupted append
```

Historical flat JSONL and other `runs/` artifacts remain intact. Schema v1 stores
IDs/timestamps, task/criteria, canonical target identity, original/observed HEAD,
round/phase/step, retries/history, model identities, pinned options, verifier,
critic and reviewer evidence references, recovery metadata and active commands.
Actual model requests record role and alias in the ledger.

`verified_progress` and `last_verified_checkpoint` are trusted state;
`unverified_work` and failure evidence are separate. A clean baseline is a
recovery anchor, **not task progress**. No model plan/summary/“done” claim is a
checkpoint. Durable logs omit model plans, summaries, reviewer free text, raw
error bodies and provider telemetry; they retain validated structured critic
findings, reviewer verdicts, tool actions and bounded command outputs/exit codes.
Hidden reasoning/raw model responses are not persisted. User task and repository
or test contents remain user data and are not filtered as model reasoning.

## State and checkpoint protocol

`CREATED -> PLANNING -> EXECUTING -> VERIFYING -> REVIEWING -> VERIFYING -> CHECKPOINTING -> VERIFIED -> COMPLETED`

Retries return to EXECUTING. Exceptions record FAILED; caught cancellation records
INTERRUPTED. Abrupt death leaves the last committed phase, and recovery records
the interruption. Each round's evidence is retained; unreferenced candidate
checkpoints never authorize progress.

Events have a sequence and SHA-256 hash chain. A transition appends and fsyncs a
complete `state_committed` event containing the new state, then atomically
replaces `state.json` through a flushed sibling temporary file. The completed
ledger record is the commit point. Valid ledger state ahead of the cache is
replayed; an initial missing cache can be reconstructed. Malformed or divergent
existing state is rejected. An unterminated final event is archived before tail
removal; corruption in a complete record is never skipped.

A checkpoint requires the existing Workflow success gates plus complete
fingerprints at verification/review boundaries. It binds canonical repo and Git
directory identity, HEAD/branch, exact tracked/untracked status, staged-index
hash, and content hashes/modes of **all tracked and non-ignored untracked files**.
It references both verification passes' commands/exit codes, reviewer approval
and identity, critic evidence/status when enabled, cleanup, round/step and time.
Any observed change during verification/review invalidates that gate. Critic
failure remains advisory; verification, authoritative review and cleanup decide.

## Boundaries and remaining risks

An OS lock serializes durable controllers sharing this harness storage root and
releases on death/reboot. Normal `run` does not participate: do not run it against
the same checkout or dedicated gateway concurrently. Recovery refuses live or
ambiguous residual children; existing one-model-at-a-time and timeout guards remain,
including 300 seconds per verifier command.

Launch intent, boot, nonce and PID/creation/executable identity are persisted.
A still-running child blocks resume; PID reuse never authorizes a signal. Death
in the launch-to-identity gap leaves a closed broker gate and UNKNOWN state.
Inspect and recover manually; there is no unsafe override. Target tests remain
trusted code. Phase 2A contains verifier descendants in broker-held Windows jobs.

Flush/replace protects process-death recovery; directory fsync is used where
available. Windows/filesystem/hardware power-loss guarantees still apply.
Checksums detect accidental corruption, not hostile rewriting of the entire
evidence store. Ignored repository files and arbitrary external services remain
outside repository fingerprints. Phase 2A fingerprints relevant runtime/config/model
evidence and requires revalidation or refusal on meaningful environment drift. Submodules
are refused and existing symlink/reparse restrictions remain. Keep the checkout
exclusive during execution: snapshots cannot make arbitrary external edits
transactional. Back up run evidence together with the checkout.

## LongHorizon study and Phase 2

Reference: [AMAP-ML/LongHorizon-Harness](https://github.com/AMAP-ML/LongHorizon-Harness),
commit `a1dd930614972b92361c1b9cd6aac441a6db5a65`. Study covered README role/loop boundaries,
`src/lh_harness/manager.py` round reconstruction/failure/artifact handling,
`types.py` round/audit records and role budgets, and `environment/local.py`
timeouts/cleanup. The reference was not installed, modified, or executed.
No implementation sections were copied; we claim no compatibility.

Adopted: durable original goal, round ledger, fresh contexts, explicit failure
evidence and verified-checkpoint recovery. Different: exact Git fingerprints,
deterministic gates, strict versioned state/WAL, and refusal of unexpected edits
or corrupt complete ledger records. We do not use natural-language audit text
as trusted progress or preserve a full agent transcript.

Future mapping: controller owns Manager responsibilities; Qwen remains
executor/repairer; deterministic verification plus GPT-OSS arbitration form the
authoritative audit boundary. Nemotron remains an advisory defect scout, never
an authoritative auditor. One large model at a time remains mandatory.

Recommended Phase 2B: use the Phase 2A supervision/environment boundary to add a
bounded Manager choosing one step and acceptance contract
from original task, verified checkpoints, remaining work and failure evidence.
Give it total time/round/retry budgets and a stall policy. Keep Executor contexts
fresh, require independent gates per checkpoint, and test multi-checkpoint
recovery without reset/merge authority. GUI, dashboards and integrations remain
outside this work.

## Deterministic validation

```powershell
python -m unittest discover -s tests -v
python -m py_compile harness.py critic.py durable.py winprocess.py tests/test_durable.py scripts/smoke-durable.py
git diff --check
python scripts/smoke-durable.py
```

The smoke uses stand-in model responses with real edits, Git, test subprocesses,
CLI dispatch and persistence. Its first process exits with code 73 immediately
after checkpoint acceptance, before the final report. A fresh process resumes
without model access and reconstructs the same checkpoint. Evidence remains
under `runs/durable-smoke-*/`.


## Bounded critic evidence and stage deadlines

Autonomous critic input retains the configured 6,000-character safety bound. The
controller constructs a canonical packet from the original task, step contract,
criterion/check state, complete changed-path/scope manifests, checksum-validated
verification and recovery artifacts, environment drift, trust/checkpoint state,
and actual repository diff. Text excerpts carry original/omitted character
counts and hashes; every diff file header remains represented. Excerpt reduction
is deterministic. Mandatory manifests/statuses are never removed to fit: a packet
that cannot fit fails closed. The packet is persisted as `critic-input.json`;
critic evidence records the canonical input size, bound and truncation state.
Nemotron remains advisory, and missing/unavailable critic evidence cannot satisfy
three-model checkpoint policy. Reviewer input remains independent.

Each planner, executor, verifier, critic and reviewer stage receives the remaining
round/total-runtime budget. Verifier command timeouts are capped by that remaining
budget. Gateway connections have an absolute stage deadline, including trickled
HTTP headers/body, in addition to socket timeouts. Durable round stage records
identify start/completion time, available budget, elapsed time, boundary and
outcome. Stages that return at or after the deadline fail closed with the stage
name; protected process/model cleanup still runs with its existing budget.

Only after the last required trust stage finishes within the execution deadline
may checkpoint bookkeeping use a separate, bounded 30-second finalization reserve.
No further trust stage may start in that reserve. Total runtime still bounds
finalization. Repository/environment and all original checkpoint gates remain
required; a checkpoint is refused if finalization exhausts its reserve. This does
not raise the 1,800-second default round budget or any model/executor timeout.
