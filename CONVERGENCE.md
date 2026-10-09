# Autonomous round recovery

This repair uses [AMAP-ML/LongHorizon-Harness](https://github.com/AMAP-ML/LongHorizon-Harness)
as an architectural reference, audited at
`a1dd930614972b92361c1b9cd6aac441a6db5a65`. It is an independent implementation;
no upstream runtime was vendored.

## Diagnosis and transitions

Run `private-historical-run` failed pytest collection: the generated
`tests/test_report.py` had an unterminated string at line 67. The first round
remained untrusted. The old low-risk `select_step` shortcut then replayed the
cached `gen-report-struct` step without calling the planner. Subsequent episodes
repeated whole-file writes and bad anchors without useful observations. The
existing failed-round budget stopped the run safely, but did not drive repair.

The confirmed untracked task files were archived with their hashes in ignored
`runs/convergence-repair-audit-075335336c05/`, then only those two files were
removed. The harness baseline HEAD was preserved. This repair implements no
operator-report feature.

The round transition is now:

* **PASS:** fresh executor candidate -> deterministic verification -> required
  critic/reviewer -> final deterministic verification after model gates ->
  trusted checkpoint -> next plan from accepted state.
* **FAIL:** executor/tool termination, timeout, verification or review failure ->
  checksummed bounded failure evidence -> no trusted progress -> fresh planner
  call -> evidence-bound repair step -> fresh executor episode.

Low risk never permits cached failed-step replay. A repair keeps the original
task and check contract, binds `recovery_from` to the latest failure's `round`
and `fingerprint`. The normalized step-content fingerprint (goal, allowed scope,
checks) may stay the same when the bounded goal is still correct. Fresh planning
is mandatory; recovery eligibility is checked separately against material failure
evidence, scoped workspace content/modes and the accepted checkpoint. The controller
cannot prove that a model's strategy is sensible. Remaining lineage budgets and
stall checks bound ineffective proposals.

Repair steps share the original attempt lineage, so renaming goals cannot
evade `max_step_retries`. The preceding risk floor survives repair planning;
declaring retained candidates low risk cannot remove previously required
review gates. Configured verification and regression checks are unchanged.
An explicitly required or already invoked reviewer is also retained across
repairs of its rejected/unaccepted candidate. A new proposal below the retained
risk floor or clearing required `needs_reviewer` is rejected explicitly; it must
be corrected by the planner before dispatch.
Interrupted unfinished work retains the explicit `verify_only` resume path;
if that verification fails, the following round plans a repair from evidence.
Safety violations involving environment/process ownership still stop for human
action instead of attempting autonomous recovery through an unsafe condition.

## Canonical failure evidence, version 1

`rounds/NNNN/failure-NNNN.json` is an object referenced with a checksum by
`manager.last_failure.reference`. It contains these exact fields:

| Field | Meaning |
| --- | --- |
| `schema_version` | `1` |
| `trusted_progress` | Always `false` |
| `round`, `step_id`, `step_fingerprint` | Failed round and bounded step identity |
| `previous_failure_reference` | Prior cause when a repair proposal fails before dispatch; otherwise null |
| `classification`, `detail`, `fingerprint` | Controller reason, bounded diagnostic, normalized failure identity |
| `recovery_state_fingerprint` | Material failure identity excluding round numbers and observation timing; rejected planning carries its underlying cause |
| `commands` | Last three failed command observations |
| `failed_tests` | Up to ten detectable pytest/unittest names |
| `targeted_check` | Safe suggested pytest file check, or null |
| `changed_paths` | Up to twenty cumulative paths differing from the trusted snapshot |
| `attempted_actions` | Last sixteen action/path/index observations; no generated source |
| `tool_failures` | Last eight action/path/classification/safe_reason observations |
| `last_successful_observation` | Latest completed read hash/count, file-list hash/count or command observation; a completed command can have a failing exit code |
| `implementation_changed` | Whether this round actually changed files, reconciled even if the executor raised |
| `trusted_checkpoint` | Last accepted checkpoint reference, or null |
| `recovery_hint` | Deterministic inspection/collection/check repair guidance |
| `evidence_truncated` | True when evidence was reduced to meet the bound |

Each command observation has `argv`, `exit_code` (null for forced timeout),
`stdout`, `stderr`, `output_truncated`, `duration_seconds`; durable failure
commands also carry `check_id` when matching a pinned check. Executor streams
are capped at 800 characters each, durable failure streams at 600, and the
last-observation streams at 180. Text removes control/ANSI sequences, omits
reasoning-tagged output and redacts obvious credential assignments. Treat all
output as untrusted data: these filters do not establish truth or defeat every
possible prompt injection.

Severely reduced command arguments carry `argv_truncated: true`; the truncated
observation is never executed as a command. Context reduction can omit the
last observation while retaining the failure command and binding, with
`evidence_truncated: true`.

The evidence object is bounded to 6,500 serialized characters. Reduction keeps
valid JSON and the latest failure binding, drops older list entries and shortens
streams with explicit markers. The planner/executor context is at most 14,000
characters; evidence may be further reduced to fit. A stable task contract that
cannot fit fails closed rather than silently dropping original requirements.
Full command evidence remains in the existing durable verifier artifacts.
Legacy failures without the new artifact use an explicitly version-0 context.

## Fresh context and untrusted workspace

Every `Workflow.implement` call starts with two messages and new progress state.
Only the pinned original task, criteria/checks/scope, contract hash, accepted
checkpoint/check progress, remaining work, current bounded step, latest failure
object and workspace metadata enter the round. Previous assistant transcripts
and executor summaries never become durable planning inputs or accepted state.
Within an episode, bounded tool observations remain available to subsequent
decisions. A rejected truncated response is still discarded before correction.
Existing focused-edit schema/path checks and repeated-read failure guard remain.

Critic/reviewer adapters receive the stable task/check/scope contract and current
verification/diff evidence, without the prior executor failure history. This
keeps audit independent and avoids using review context limits on old failures.

Failed candidates survive in place as explicitly **untrusted**. The next
context lists their paths and snapshot fingerprint. Existing candidate files
must be read before mutation in a recovery episode. The repair scope must cover
all retained changes for cumulative scope/review gates to pass. Reads, test
results and model summaries are observations; only the existing acceptance gates
can promote files to trusted progress. No broad reset is used.

## Exact anti-churn and command policy

* At most **two successful mutations per path without a useful observation**.
  The third attempt is blocked before the write with `observation_required`.
  There is **one** forced-observation opportunity per episode, across all paths.
  Its very next action must read that path, run a relevant deterministic check
  or inspect a relevant Git diff. Other actions, malformed responses, or an
  incomplete required check stop `NO_PROGRESS`. A second exhausted mutation
  window stops immediately. Reads reset only that path; checks reset statically
  relevant paths (including test imports); heartbeat/progress output and file
  listings never reset the window. The blocked edit is not queued or replayed.
* No-op writes are failures. A failed `replace_text` exposes `anchor_not_found`
  or `anchor_multiple_matches` and requires a fresh read before another mutation
  of that path. A mutation without that read terminates with `NO_PROGRESS`.
  Progress keys normalize relative path aliases and Windows case, so `./calc.py`
  and `CALC.py` cannot evade the same-file limit.
* Repeating `list_files` with the same file-list hash and mutation revisions
  terminates with `NO_PROGRESS` on the second call. A file listing does not reset
  the per-file mutation-without-observation counter.
* Two failures with the same action/path/classification/current-content hash
  terminate with `NO_PROGRESS`, even when the model changes the bad anchor or
  inserts a read. Content changes give a genuinely new failure state.
* Repeating an identical observed command without a relevant mutation stops
  before launching another subprocess. For Python targeted checks, relevance
  includes named test paths and statically parsed imports; full/non-Python checks
  conservatively depend on all mutations. Dynamic dependencies may require a
  fresh episode/replan; no imported code is executed to infer relevance.
* `done` after a failed check following the latest mutation terminates with
  `KNOWN_CHECK_FAILED`. `done` remains an untrusted candidate stop request;
  Manager verification still runs independently on eligible candidates.
* The existing two-consecutive-invalid-action stop, action limits, round/step
  lineage/runtime/stall limits, command deadlines and process cleanup remain.

Repair prompts recommend the smallest relevant check before the full suite.
The suggestion never replaces the operator-pinned acceptance commands. Model
request, per-round and verification timeout failures remain classified
separately; a process-ownership or cleanup failure cannot be treated as safe
recovery evidence to bypass supervision.

Chat HTTP errors retain only their status and become role-specific request
failures for replan; malformed planner/reviewer wire shapes also fail their
round. Startup, ownership, persistence and cleanup errors retain the existing
hard-stop handling rather than being swallowed as ordinary chat failures.

## Live planner contract repair

Run `private-historical-run` reached fresh planning correctly after round
1 `NO_PROGRESS`. Both round 2 and round 3 failed the **parser-selected object's
step field-set check**, before recovery binding, substantive fingerprint, scope or policy
validation. Their failure artifacts record the same exact required set including
`recovery_from`; request durations were 14.17 and 11.73 seconds. The historical
run did not record the actual field set, response content/length, finish reason
or token counts. A specific missing/extra field or truncation therefore cannot
be established retrospectively. Since the old parser salvaged embedded objects,
malformed surrounding JSON or truncation cannot be ruled out either.
Missing `recovery_from` is consistent with the
old weaker example, but remains a hypothesis, not an observed wire fact.

The baseline planner prompt showed only the nine-field initial object while its
prose required a tenth recovery field. Manager requests had no `response_format`,
and the parser searched for embedded JSON objects. The contract was not aligned.
The confirmed candidate files were archived by snapshot-matching hashes under
ignored `runs/planner-repair-audit-e1ba5716e5c2/` and only those files removed;
the same directory contains the bounded historical contract audit.

`planner_protocol.py` is now the canonical contract module. `schema(context)`
generates the exact schema used by the prompt, `manager_step` JSON-schema wire
grammar and parser shape validation. All nine original fields remain required.
When failure evidence identifies a failed step, the tenth field is mandatory:
`recovery_from = {"round": latest_round, "fingerprint": latest_fingerprint}`.
Both values are exact schema constants; booleans cannot stand in for integers.
Retained risk narrows the allowed risk enum; a required reviewer makes
`needs_reviewer` exactly true. Pinned check IDs, text/array limits and field sets
come from the same schema. Extra/missing fields, duplicate keys, wrappers,
trailing text, nested-object salvage and nonfinite JSON are refused. A wire
finish reason other than `stop` is refused even if its text happens to parse.
Controller scope confinement, unproven-check intent, safe text and evidence-bound
recovery replay validation remain additional authoritative gates.

Planner context includes deterministic `planning_constraints`: retained risk
and reviewer floors plus the prior failed goal/scope/checks. The original
`task_contract_sha256` remains stable. These inputs describe recovery; they do
not create progress or let the model change pinned commands.

Production ModelPlanner supports at most **two attempts per planning tier per
round**: an initial request and one fresh correction. Only deterministic
validation failures permit correction. It receives the same bounded context,
canonical schema and a `plan_correction` containing the safe classification and
canonical invalid field paths. No rejected response or transcript is replayed,
and no semantic output is silently fixed. HTTP/request failures do not trigger
this protocol loop. Each attempt charges the existing role-invocation budget;
runtime/round limits and process ownership checks apply before requests and the
time limit is rechecked after validation. A high-risk senior planning tier has
the same two-attempt bound and retains the code proposal's risk floor. Scripted
adapters opt in explicitly; their default remains one request.

A corrected plan stays in the same PLANNING round and does not count as a failed
implementation. Exhausted invalid planning ends the round untrusted and uses
the existing consecutive-failure/stall/round budgets. Fresh replanning from new
failure evidence remains mandatory; cached retry is not reintroduced.

Normal diagnostics preserve content-free wire shape, finish reason, numeric
usage, schema version/hash, prompt/response character counts, known field
names/types and unexpected-field counts. Per-attempt `plan-executor-NN.json` /
`plan-reviewer-NN.json` records parse/schema/recovery/substantive/policy-floor
results with explicit classifications. Unknown model field names and field
values are omitted. No arbitrary response preview or reasoning is retained.
`not_reached` is distinct from failed validation. The dedicated live smoke alone
captures explicitly bounded synthetic wire pairs in ignored evidence.

`scripts/smoke-manager-recovery.py` uses real Qwen ModelPlanner for both plans,
then a fresh real Qwen executor and independent deterministic verification in a
temporary target. The older executor recovery smoke still uses its diagnostic
planner and is not evidence of live planning quality. The new smoke has no
operator-report input and does not claim three-model live coverage.

The final live wire also demonstrated a model-quality limit: its repair goal
correctly described fixing addition, but `completion_signal` copied the prior
failure wording. This free-text field remains an untrusted description, never a
verification waiver. The fresh executor repaired the file and both its targeted
check and Manager's independent checks passed before the fixture checkpoint.
The schema/fingerprint gates establish contract validity, not perfect natural
language quality; no phrase-based semantic auto-fix was introduced.

## LongHorizon reference audit

### Mutation and same-goal recovery audit, 2026-10-01

Run `private-historical-run` demonstrated an unintended over-constraint:
round 2 performed a write and a successful replace after its reads, then the
third mutation was stopped before dispatch, with no observation opportunity.
Round 3 really called Qwen planning twice and supplied the latest round-2
failure binding. Both proposals repeated the normalized goal/scope/check intent
and were rejected solely as `unchanged_substantive_step`. The two different
failure fingerprints were `dfbd5dbb...` (repeated anchors) and `648ee842...`
(mutation churn); the later planner rejection consumed the third failed round.
No verifier evidence or trusted checkpoint existed.

The only retained task artifact was untracked `report.py`. Its SHA256 matched
the final untrusted snapshot (`b7735b486dff101a916328acd2c0f23a72cffa281b589cbbf6a217b0bdafa3ad`).
It was archived with a manifest in ignored `runs/convergence-policy-audit-20261001/`
and only that confirmed candidate was removed. Harness HEAD stayed
`38641024eaf8348b4d389869c6e7e53b7840a28d`; no task feature was implemented.

The current upstream [manager loop](https://github.com/AMAP-ML/LongHorizon-Harness/blob/main/src/lh_harness/manager.py)
rebuilds its Manager prompt every round from the original task, maintained state,
contract and audit/failure feedback, then calls a new role episode. Invalid
routes create auditable feedback and return to planning under the round bound;
valid CLI/GUI plans get new executor episodes. Its
[task-contract rules](https://github.com/AMAP-ML/LongHorizon-Harness/blob/main/src/lh_harness/prompt_texts.py)
keep the target stable and require environment evidence and independent audit.
There is no mandatory goal/scope/check fingerprint change gate in this reviewed
loop. Our fresh planning and failure evidence approach aligns with that model;
the old mandatory substantive change was an accidental over-constraint of the
cached-replay repair, rather than an upstream requirement.

`step_fingerprint` remains the normalized **step-content identity**. A separate
controller-computed **recovery-attempt identity** combines that content with the
material failure-state fingerprint, scoped workspace file hashes/modes, and
accepted checkpoint reference. It excludes step IDs/rationale, binding round
numbers, timing and unrelated workspace changes. Exact latest `recovery_from`
binding remains required independently. Same content plus new material failure
or scoped content can be eligible; an already attempted identical recovery
identity is refused before executor dispatch, even after a cosmetic rename.
This does not prove semantic strategy quality: the prompt explicitly asks the
planner to respond to evidence and change strategy, not invent goal/scope/check
changes. Lineage budgets bound weak strategies.

Executor input now explicitly carries the current episode kind, validated latest
`recovery_from`, and bounded Manager rationale as an **untrusted strategy proposal**
under the pinned task/scope/check contract. This delivery matters when the goal
stays the same. The first real diagnostic (`observation-recovery-smoke-0d502ff9fd40`)
accepted a same-goal plan whose rationale correctly diagnosed signed addition,
but the old `contract_text` omitted both binding and rationale. Qwen repeated
the initial boundary probe in its fresh second episode. Deterministic negative
verification rejected both rounds, no checkpoint was created, and cleanup passed.
The wire proves the omitted fields; it does not prove that omission was the sole
cause of the model's behavior. A new deterministic delivery/isolation regression
and a second live diagnostic check the repaired contract. Critic/reviewer inputs
still exclude recovery strategy/history so their independent evidence remains
separate from the executor's repair instructions.

Material failure identity includes normalized reason/detail and deterministic
failed command outputs/arguments plus tool failure classifications. Planner
rejection carries the underlying failure identity forward, so new rejection
rounds cannot manufacture eligibility. Revisited workspace alone is not a stall
when failure evidence is materially new; repeated material failure and revisited
failure/workspace pairs remain stall signals. The recorded raw binding fingerprint
is retained separately for diagnosis and exact binding. No heartbeat or model
summary enters these identities or establishes progress.

The forced-observation rule is canonical in `recovery.executor_progress_policy`,
shared by regular and focused-edit prompts and the controller's numeric limits.
Tool success after mutation recommends read/diff/targeted testing; a failed
literal anchor still requires a fresh read and repeated failed anchors against
unchanged content still stop immediately. The new feedback is not counted as
malformed protocol output and never executes or queues the rejected edit.

The existing `max_consecutive_failed_rounds=3` policy is intentionally retained.
An exhausted planning contract round still consumes bounded calls and runtime;
one protocol correction already remains inside PLANNING without consuming an
additional failed round or executor attempt. Separating counters would grant
extra autonomous opportunities, not fix the incorrect eligibility gate. Upstream
also bounds invalid planning using its round loop; it does not establish an
equivalent three-failure constant. Our additional conservative failure/stall and
retry lineage bounds remain a deliberate local safety policy.

The realistic live diagnostic is `scripts/smoke-observation-recovery.py`:
real Qwen initial plan -> read -> two mutations -> blocked third mutation ->
required read -> focused correction -> positive targeted PASS -> independent
negative test FAIL -> fresh real Manager plan with the same bounded goal and
latest binding -> fresh real executor -> focused correction -> targeted PASS ->
independent verification -> fixture checkpoint. All tests remain immutable and
outside write scope. Its bounded synthetic wires stay ignored; the target is
removed and models unloaded. It does not exercise live Nemotron/GPT-OSS or the
operator-report task; their trust/authority paths remain deterministic regressions.

The final real diagnostic (`runs/observation-recovery-smoke-f3e3d3c08dc6/result.json`)
passed with **14 actual Qwen calls**, two real Manager plans and two fresh executor
episodes. Every wire finish reason was `stop`. The action sequence included two
successful mutations, one blocked third mutation, immediate same-path read,
focused correction and targeted PASS. Independent negative-input verification
then failed the first round. The second plan retained the exact normalized
goal/scope/check fingerprint, bound the failure, and its new executor completed
one focused correction and a positive/negative targeted PASS before independent
verification and the fixture checkpoint. Both initial Planner requests and both
Executor episodes started with two messages; task-contract hashes matched.
Tests were preserved, supervisor idle, temporary target removed and both gateway
model/native server inventories empty. All runtime source SHA256 values match
the smoke's pinned environment. Wire pairs were at most 17,077 bytes each and
remain under the ignored directory. This proves the controlled live path, not
arbitrary model planning quality or live three-model coverage.

Final validation: convergence/planner/liveness/Manager replay targeted suites
**75 passed, 32 subtests passed**; after the live delivery fix, executor delivery,
independent audit isolation and executor protocol/payload/recovery tests
**24 passed, 41 subtests passed**. Policy/gate/three-model/verified-existing and
executor targeted checks also passed (**87 passed, 76 subtests passed**).
Complete final suite: **439 passed, 230 subtests passed in 831.87 seconds**, exit 0,
using `python -m pytest -o addopts= -q --durations=12` on the unchanged final
runtime. The first full run found only an outdated test expectation: carrying
the original failure through invalid planning stopped cached replay one round
earlier; the regression now asserts that stronger bound. Windows owned-process
tests ran outside the restricted shell sandbox. Logs and synthetic artifacts
are ignored; no harness commit or operator-report run was performed.

The relevant sources are upstream
[manager.py](https://github.com/AMAP-ML/LongHorizon-Harness/blob/a1dd930614972b92361c1b9cd6aac441a6db5a65/src/lh_harness/manager.py),
[role_prompts.py](https://github.com/AMAP-ML/LongHorizon-Harness/blob/a1dd930614972b92361c1b9cd6aac441a6db5a65/src/lh_harness/role_prompts.py)
and [prompt_texts.py](https://github.com/AMAP-ML/LongHorizon-Harness/blob/a1dd930614972b92361c1b9cd6aac441a6db5a65/src/lh_harness/prompt_texts.py).

| Principle examined | Local adoption / intentional difference |
| --- | --- |
| Manager round reconstruction | Reconstruct from durable original task, accepted progress, latest failure and current candidate metadata; no opaque conversation replay |
| Bounded next step | Strict existing machine schema plus explicit recovery binding; retain pinned check IDs and scope confinement |
| Fresh executor episode | New Workflow messages and episode state for every round |
| Stable task state / contract | Immutable pinned inputs and digest, rather than a model-authored evolving task contract |
| Failure evidence | Structured bounded command/action/tool observations; do not import upstream free-form role histories |
| Independent auditor isolation | Preserve separate review inputs and snapshot binding; critic/reviewer do not receive executor reasoning or authority to write trusted state |
| Trusted intermediate state | Existing deterministic, scope, environment, ownership and checkpoint gates; do not adopt auditor narrative as ground truth |
| Timeout recovery | Record recoverable request/round/check failures for fresh planning; preserve hard stops on unsafe ownership/cleanup |
| Completion gating | Keep all pinned acceptance evidence mandatory; model completion claims never complete a run |

The explicit three-model policy remains `--critic autonomous-run --reviewer
--review-policy three-model`: Qwen -> deterministic verifier -> completed
Nemotron -> approved GPT-OSS -> final deterministic verifier -> checkpoint.
Risk-based defaults and high-risk senior planning remain. No critic or reviewer
can waive failed deterministic evidence.

## Validation on 2026-10-01

Focused convergence/protocol/payload/recovery/hardening tests: **81 passed,
64 subtests passed**. Complete suite on the final runtime: **394 passed,
202 subtests passed in 735.76 seconds**. The Windows process tests were run
outside the restricted shell sandbox so they could terminate their owned trees.

Real Qwen recovery smoke: `runs/executor-recovery-smoke-d3ec6b520580/result.json`,
**PASS**, seven real completion requests, all `finish_reason=stop`:
`write_file -> targeted FAIL -> done refused -> fresh episode -> read_file ->
replace_text -> targeted PASS -> done -> independent verification -> fixture checkpoint`.
The first and repair wire requests each have exactly two initial messages;
the stable contract hash matches across rounds. Tests were preserved, supervisor
was idle, temporary target was removed and gateway/native servers were empty.
Raw synthetic pairs remain in `wire-01.json` through `wire-07.json` under the
same ignored directory. The first live smoke exposed a unittest summary being
mistaken for a test name; the final parser and regression exclude that summary.

The live planner is a deterministic diagnostic fixture. This proves live executor
recovery with production orchestration, not arbitrary model planning quality.
Three-model gates and final-verification refusal paths were tested
deterministically; this live smoke deliberately used only Qwen.

## Live Manager repair validation on 2026-10-01

Targeted planner/Manager/convergence/executor/verified-existing tests:
**173 passed, 108 subtests passed in 565.47 seconds**. The final canonical
protocol/step-contract check, including senior-planner correction and diagnostic
event separation: **21 passed, 36 subtests passed in 21.86 seconds**.
Complete final suite: **408 passed, 211 subtests passed in 749.70 seconds**,
exit code 0. Windows process tests ran outside the restricted shell sandbox.

Final real-Qwen Manager smoke:
`runs/manager-recovery-smoke-58cbc576186a/result.json`, **PASS**, nine actual
completion requests (two real Manager plans, seven executor calls), all
`finish_reason=stop`. Both plans passed on their first attempt; the one-correction
path was exercised deterministically. The sequence was initial plan -> seed
write -> targeted FAIL -> done refused -> fresh real recovery plan -> fresh
executor read -> literal replace -> targeted PASS -> done -> independent
unittest + `git diff --check` PASS -> round-2 fixture checkpoint -> COMPLETED.
The real recovery plan bound round 1/fingerprint exactly, changed the substantive
fingerprint and retained the original task/check contract and risk floor. Its
copied failure-worded completion signal remained untrusted as described above.
The fixed test was preserved, temporary target removed, supervisor idle and
gateway/native server inventories empty. Current runtime source hashes match
the smoke's pinned environment; bounded wire evidence remains ignored under
`wire-01.json` through `wire-09.json`. This live test exercised Qwen only;
required reviewer retention, critic/reviewer authority and three-model final
verification were covered by deterministic regressions.
