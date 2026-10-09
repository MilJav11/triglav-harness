# SafeQwen public integration (Phase 8A)

Experimental, explicitly selected integration on native Windows, with offline
validation only. This migrated code has **not** undergone live qualification.
Cloning TRIGLAV does not reproduce historical TQ-02R. No model weights, gateway,
native binaries or retained qualification fixture/evidence are included.

## Components and authority

| Component | Responsibility |
|---|---|
| `terminal_run.py` | Existing autonomous CLI route; validates operator policy/configuration, creates one fixed WorkUnit and injects the worker |
| `safe_qwen_worker.py` | Bounded read/exact-edit/submit conversation and file policy |
| `tool_protocol.py` | Closed, duplicate-key-rejecting JSON schema/dispatcher contract |
| `integration_shim.py` | Existing supervised Gateway transport, response validation, bounded telemetry, deadline and cleanup |
| `safe_executor_binding.py` | Source/config/scope identity, direct environment/executor binding, configured gateway ownership resolver |

The original custom executor remains the default. OpenCode stays opt-in; the
historical Cline adapter is retained. SafeQwen uses no planner or model-authored
scope: an operator supplies a fixed task/file policy and verifier commands. One
worker attempt, 1–32 operations, and a 1–600 second unit deadline are supported.
The existing Manager, scheduler, deterministic verifier, critic, reviewer,
checkpoint and Windows delegated-child ownership gates remain authoritative.
There is no global monkey-patching or plugin discovery.

| Enabled gates | Result after successful worker/verifier |
|---|---|
| Neither critic nor reviewer | READY / MILESTONE_READY; no trusted checkpoint |
| Critic only | Intermediate critic result; no trusted checkpoint |
| Critic and reviewer | Existing review policy, fresh final verifier, candidate/identity stability, controller checkpoint; human semantic audit still required |
| Reviewer without critic | Refused |

The fixed WorkUnit route does not use step-based risk planning: enabled critic
and reviewer gates run after successful unit verification.
`three-model` requires both `--critic` and `--reviewer`. The critic remains advisory;
reviewer APPROVE cannot override failed verification or independently authorize a
checkpoint. Worker submit and zero exit remain untrusted.

## Windows prerequisites and configuration

Use Python 3.11+, Git, a committed TRIGLAV checkout, and a separate clean committed
Git target repository with trusted tests. The new Python modules use only the
standard library and included controller modules. Provision a compatible local
llama-swap gateway, llama.cpp-compatible server(s), and licensed model weights
separately. The inherited Windows supervisor expects one native llama-server at a
time and rejects unrelated/ambiguous resident processes. This is not a generic
remote OpenAI service adapter. No automatic gateway startup is provided.

1. Copy [the configuration example](../config/safeqwen.example.json) and
   [exact-file policy example](../config/safeqwen-policy.example.json) to operator
   configuration files. Edit every runtime filename, alias, endpoint, deadline,
   RAM threshold and policy entry for your deployment. Examples are not supplied
   runtime assets or qualified hardware settings.
2. Provision/start a **dedicated** loopback gateway yourself. Its `safe_qwen_gateway`
   binary must match the running parent process; its operator-owned `pid_file`
   must contain that process's decimal PID. Never use another service's PID.
   Gateway deployment and final gateway shutdown belong to the operator; the
   controller owns supervised model switch/unload operations. The new launcher
   neither starts nor kills the gateway process.
3. `safe_qwen_runtime` must declare exactly the enabled roles (`code`, plus
   `critic` and/or `review`). Each role requires `binary`, `model` and
   `gateway_config`; all roles must share the same gateway configuration file.
   Paths resolve relative to the operator configuration file, or may be explicit
   local paths. Network/device paths are unsupported. Gateway ownership inputs,
   source files, binaries and gateway configuration use SHA-256; weights use
   size/mtime metadata, **not** full model-content hashes.
4. The gateway YAML must use a supported, controlled command shape. The configured
   role alias appears once as a two-space-indented model key, followed by
   `cmd: >-` and a single command line starting with a quoted executable and
   containing exactly one quoted `--model` path. Both executable and model paths
   must be absolute local paths matching the declared runtime resources. For
   example, substitute real provisioned paths in this schematic:

   ```yaml
   models:
     qwen-code:
       cmd: >-
         "<absolute-local-llama-server-path>" --model "<absolute-local-model-path>" --port 8091
   ```

   PID alone is insufficient: the supervisor checks executable/creation identity,
   parent lineage and delegated child identity before authorizing lifecycle work.
   Unsupported command formats or ownership ambiguity fail closed. Operator
   declarations are not independent proof that a live gateway loaded those bytes;
   verify actual deployment in Phase 8B. Gateway configuration containing secrets
   should stay outside the repository. Never publish local runtime logs/evidence.
5. Implicit `review_profiles`/HotPin template deployment is unsupported for this
   route and refused. Provision explicit role commands instead; their live
   compatibility remains unqualified. Use operator configuration without the
   inherited machine-specific startup templates.

## Actual CLI

From the TRIGLAV checkout, after adapting the examples and provisioning local
runtime files, validate without contacting the gateway or creating a run:

```powershell
python -B harness.py --config config/safeqwen.local.json autonomous-run `
  --executor safe_qwen --safeqwen-policy config/safeqwen-policy.local.json `
  --repo ../disposable-calculator --task "Implement multiply in calculator.py" `
  --verify '["python", "-m", "unittest", "test_calculator.py"]' `
  --acceptance "multiply returns the correct product" `
  --allow calculator.py --forbid test_calculator.py --dry-run --quiet
```

`--allow` must exactly match writable policy files in mutation mode; `--forbid`
must exactly match the remaining readable (frozen) files. They are repeatable,
exact names, not directories/globs. Files must already exist and be UTF-8, each
at most 65,536 bytes. Do not store operator configuration inside the target scope.
Only `--max-runtime-minutes` from the legacy Manager budget flags is supported
for fixed-unit scheduling. Round/retry/stall/model-invocation overrides are refused
rather than silently ignored; use policy operation/attempt/deadline limits.
Critic/reviewer/final verification retain their separately bounded existing stages;
the scheduler runtime budget is not a whole-chain wall-clock guarantee.
Read-only mode requires empty writable policy, `--allow` equal to readable files,
and no `--forbid`: its entire readable set is internally frozen, and the existing
read-only scheduler forbids candidate mutation.

Removing `--dry-run` is an explicit **live model invocation**; do that only under
separate Phase 8B authorization. The code-only example returns an intermediate
READY result (CLI exit 2), not acceptance. For the full gated path, add runtime
resources for `critic` and `review`, place global `--critic` before
`autonomous-run`, and add `--reviewer --review-policy three-model`. Exit 0 means
COMPLETED or validated dry-run; exit 1 means stopped/error; exit 2 is a
non-completed result. This phase executed no such live invocation.

Inspect with `python -B harness.py status --run-id <operator-run-id> --json`.
Resume through `python -B harness.py autonomous-resume --run-id <operator-run-id>`;
it reconstructs only the pinned worker configuration, validates its identity and
uses the existing environment/process/repository recovery gates. Roles cannot be
added during SafeQwen resume; start a new configured run instead. Completed
checkpoint reuse validates current candidate, review evidence and identities and
never redispatches the worker. A baseline interruption can resume within bounds.
Intermediate edits lacking the legacy `unverified_work.observed` binding are
**refused**, even when unit verification succeeded; preserve the candidate and
inspect it manually rather than resetting files or manufacturing recovery proof.

The launcher also refuses a result whose controller cleanup is unproven or whose
supervision records are not idle. The inherited controller may retain a COMPLETED
record/checkpoint alongside a cleanup-failure diagnostic; the launcher does not
rewrite those records or treat that diagnostic as a successful launch.

## Tool permissions and residual risks

Only `read_files`, `edit_file`, `submit` are exposed. No shell, command, delete,
create, network, plugin or model-lifecycle tool is exposed to the worker. Malformed
JSON, duplicate fields, unknown tools, stale hashes and non-unique replacements
are rejected. Atomic replacement requires persisted intent and confirmation;
audit/cleanup failure leaves the candidate untrusted. Transport failures have no
executor fallback or transport retry; malformed responses consume operation budget.
Late responses are rejected; interrupted execution attempts unload and restores
the prior deadline. Unproven teardown stays a failure.

Exact ASCII relative paths reject traversal, absolute/UNC/drive/device/ADS syntax,
backslashes, case aliases, reserved Windows names and protected metadata. Every
root/target component is checked for symlink/reparse status; non-regular and
multiply hard-linked targets are denied. Frozen hashes and required files are
checked around operations. These checks do **not** close every concurrent
filesystem replacement race. Require exclusive access to the target and runtime
configuration. These are application safeguards, **not an OS sandbox**.

Verification is controller-owned and executes repository tests/edited Python.
Trusted tests are required: model-written source can execute during verification,
and offline CI guards are scoped to reviewed disposable fixtures. They do not
contain arbitrary malicious repositories or all child behavior. Windows broker
Job Objects preserve their existing bounded ownership rules; native crash/cancel
windows, actual model cleanup and hardware limits require Phase 8B review.

## Offline checks and historical scope

Run only the reviewed guarded command on a committed native Windows checkout:

```powershell
python -B scripts/ci_offline.py
```

The current selection is 119 tests: 10 guard, 22 existing protocol, 39 worker,
40 integration and 8 selected existing final-verification/checkpoint regressions.
Expected terminal summary: `PASS: 119 reviewed offline cases; CLI help; ...;
model calls 0`. Responses are scripted; the real controller, deterministic fixture
verifiers and checkpoint code execute. Unmocked Gateway calls, sockets and
unreviewed process/verifier commands are denied. Imports are checked for no model,
gateway or process activity. No dependency installation occurs. All other test
modules/native smoke scripts are skipped pending dependency review; this is not a
full-suite or live-system pass. See [CI scope](CI.md).

[Source migration](SAFEQWEN_SOURCE.md) records the original five byte hashes and
separate migrated identities. The preserved TQ-02R identity matched all five
original sources before migration. Historical records describe Qwen execution,
Nemotron critique, GPT-OSS review, final deterministic verification, a trusted
checkpoint and 32 frozen fixture tests, with verdict
`CHAIN_ACCEPTED_AWAITING_HUMAN_AUDIT`. No raw record, private run identifier,
archive or historical fixture was imported. The top-level
`ALL_CRITERIA_PROVEN` versus nested AC1–AC3 `NOT_PROVEN` discrepancy remains
unresolved and disclosed. This offline migrated-code validation does not reproduce
that external deployment, establish equivalence, or constitute public-clone
qualification. See [historical qualification](triglav/QUALIFICATION.md).

## Phase 8B requirements

Owner-authorized live tests must verify the actual model/gateway/runtime versions,
licensed artifacts and declared command/parent identity, resource/RAM/deadline
settings, worker operations and scope, actual critic/reviewer policy, fresh final
verification, trusted checkpoint, unchanged completed reuse, interruption and
cleanup behavior. Use a new disposable fixture/run and independent frozen oracle;
never repair/reuse retained qualification evidence. Record new source/runtime pins
and separate human semantic review. Examine the remaining report discrepancy,
intermediate recovery limitation, filesystem races and failure/cleanup diagnostics.
No production, equivalence, host-sandbox or blanket autonomy guarantee follows
from Phase 8A.
