# Executor integration boundary (before implementation)

Baseline: 26196548023f4fe4e3a837d4a73054e74de5a04d. PoC:
Historical local PoC; OpenCode 1.18.30, source 3104c1428ec91f809e5ab86631300de41eb6952e.

- Interface: manager.ModelExecutor.execute(step, context, repo). ManagerLoop.run_step ignores
  its string return. Manager supplies validated goal/scope/check IDs and bounded build_context
  task, criteria, checks, workspace and recovery evidence. No Manager agent is added.
- Failures: ExecutorError(reason) becomes RoundFailed; request/action/timeouts are retryable
  untrusted evidence. Ambiguous process/environment/repository ownership stops for an operator.
- Workspace: ScopedRepository over DurableRepository; clean Git baseline required. Current
  custom file writes are scope gated. Shell and OpenCode permissions are not OS sandboxes.
  Snapshots and Manager's changed-path gate decide acceptance independently of executor tools.
- Deadlines: Manager owns total/round budgets; legacy tick checks between phases. Gateway
  owns model startup/request/shutdown budgets. Adapter receives an absolute execution deadline
  capped by episode timeout; cleanup has a separate reserve and never grants execution time.
- Process ownership: Supervisor persists identity before releasing a broker gate. Windows
  OwnedJob contains broker/descendants, kills them on broker death/close, and verifies drain.
  Extend this broker with argv/env/stdin transport and a separate opencode_executor purpose.
  llama-swap/llama.cpp remain delegated Gateway-owned inference, outside the executor job.
- Evidence: checksummed Store WAL, context-executor.json, repository snapshots, command/verifier
  results, bounded failure artifacts, review evidence and checkpoint snapshot bindings.
  OpenCode events are reduced to content-free episode metadata; no action grammar is imported.
- Lifecycle: Gateway.switch('code') performs RAM preflight, unload and qualified llama-swap
  model load. Existing critic/reviewer switches and final unload retain ownership.

Smallest adapter: execute(step, context, repo), optional set_deadline(absolute_monotonic).
Generate isolated pinned config and a bounded stdin contract, supervise a fresh OpenCode run,
return classification/exit/change/session metadata or raise ExecutorError. Completion is
candidate-only. Manager still independently observes and validates the candidate, then verifies,
critiques/reviews, verifies again when models reviewed it, and promotes a trusted checkpoint.
Legacy ModelExecutor/Workflow remain selectable and unchanged.

## Runtime budget and Windows snapshot repair (2026-10-04)

Pinned OpenCode 1.18.30's session/overflow.ts checks the last response's usage,
not the next assembled prompt. Without limit.input, usable() is context minus
maxOutputTokens; compaction.reserved has no effect in that branch. The old
32768/4096 configuration therefore waited until 28672 reported tokens. Tool
results inserted after that check could push the next request beyond llama.cpp's
32768-token context. Increasing the timeout cannot fix this.

The adapter now advertises context=32768, input=28672, output=4096 and sets
compaction.auto=true, reserved=8192, preserve_recent_tokens=2048. usable() is now
20480: 12288 tokens of physical context remain for newly inserted results and
request overhead, including the unchanged output allowance. Main and compaction
requests still route to the same qualified local Qwen. No runtime context increase
or model switch is introduced.

Recovery is not a hard request-size limiter: pinned compaction serializes tool
outputs with a 2000-character cap per result, but selects retained history using
an approximate characters/4 estimate. The old default retained roughly 7168
estimated tokens. The explicit 2048 estimate reduces the verbatim tail so the
summary, resumed tools/system prompts and new outputs have room. Pruning does not
solve this bounded single-turn exploration: its protection floor is 40000 tokens
and it skips the latest user turn. We leave that mechanism unchanged. A large
single read (the pinned read tool can return 50 KiB and bypass generic tool_output
truncation) or several calls in one response can still exceed the margin. Thus
these settings need bounded live confirmation; they are not a tokenizer-level
proof for arbitrary repositories/tool outputs. Any OpenCode error remains sticky
in metadata and keeps the episode untrusted, even if OpenCode later recovers.

OpenCode puts its snapshot Git directory below
XDG_DATA_HOME/opencode/snapshot/<40-char project SHA1>/<40-char worktree SHA1>.
Git for Windows setup_explicit_git_dir rejects UTF-8 path lengths over
PATH_MAX-40 (220 bytes), even with core.longpaths=true. Saved qualification logs
show a 237-byte path and empty tracking hashes: OpenCode's snapshots were actually
broken, not just noisy. Controller durable.snapshot plus repository_inventory
were independent and still detected candidate/ignored-file edits and scope
violations; OpenCode snapshot hashes never supplied checkpoint authority.

Evidence remains under controller/opencode/<full episode UUID>. Runtime data is
now under controller/oc/<first 16 episode UUID characters>, created exclusively
for each episode. The adapter checks the full snapshot suffix against 220 UTF-8
bytes before launching OpenCode or loading Qwen. Deep/non-ASCII controller paths
that still exceed this budget fail OPENCODE_START_FAILED with an instruction to
use a shorter controller evidence path. Runtime data stays outside the candidate;
snapshot tracking remains explicitly enabled. No shared/global data directory,
warning suppression, Git index mutation or trust-gate change is used.

Deterministic regression: tests/test_opencode_runtime.py runs the installed pinned
binary and the existing Windows broker against a scripted localhost HTTP server,
without llama-swap or model loading. Enable HARNESS_TEST_OPENCODE_RUNTIME=1 when
running this test. It reproduces the legacy oversized-request transition and its
sticky failure, then proves the repaired threshold compacts before that request,
resumes, edits calc.py and runs the acceptance command. It checks actual OpenCode
snapshot content and absence of snapshot warnings, candidate HEAD/index stability,
4096 wire output limits and the broker's terminal cleanup receipt. Ordinary
OpenCode tests exercise Windows Git's legacy failure, snapshot modification/addition/
deletion tracking, UTF-8 path rejection before launch, scope failures and the
unchanged verifier/critic/reviewer/checkpoint gates.

Repair validation (2026-10-04), run from the executor worktree with the existing
the development virtual-environment Python interpreter and -B:
- Full suite: -m pytest -o addopts= -q --durations=10; 465 passed, 2 opt-in
  runtime tests skipped, 230 subtests passed in 701.87 seconds.
- HARNESS_TEST_OPENCODE_RUNTIME=1: -m pytest tests/test_opencode_runtime.py
  -o addopts= -q --durations=4; both pinned runtime regressions passed in 24.66 seconds.
- git diff --check and the new-file no-index whitespace check passed.
- No llama-swap, Qwen, Nemotron or GPT-OSS live qualification was started.

Primary implementation references:
- [Pinned overflow budgeting](https://github.com/anomalyco/opencode/blob/v1.18.30/packages/opencode/src/session/overflow.ts)
- [Pinned compaction selection and serialization](https://github.com/anomalyco/opencode/blob/v1.18.30/packages/opencode/src/session/compaction.ts)
- [Pinned snapshot paths and failed Git operations](https://github.com/anomalyco/opencode/blob/v1.18.30/packages/opencode/src/snapshot/index.ts)
- [Git for Windows explicit Git directory guard](https://github.com/git-for-windows/git/blob/v2.55.0.windows.3/setup.c)

## Live-informed bounded repair (2026-10-04)

Initial run private-historical-run stopped STALLED without edits.
Snapshot tracking was healthy and all owned children exited. A successful
19223-token response was below the 20480 threshold; its next default harness.py
read returned 55809 characters and the following request was rejected at 34168
tokens. Compaction completed but truncated source detail, prompting repeated
whole-file reads. The earlier scripted regression used 25000 tokens and missed
this transition.

reserved is now 12288 (threshold 16384); context/input/output and the 600-second
episode timeout are unchanged. Episode guidance requests grep plus explicit
read offsets/limits of at most 120 lines. This is guidance, not enforced tool
isolation or a hard token cap. Overflow remains sticky and untrusted.

A disposable pytest reproduction also showed ignored .pytest_cache metadata
as inventory changes outside source scope. Episode-owned PYTEST_ADDOPTS disables
only cacheprovider, preventing those writes rather than excluding ignored files
from the inventory. Tests still collect, execute and fail on assertions. The
controller's independent verifier is unchanged. No source scope, review gate,
checkpoint authority or process supervision changes are made.

The pinned runtime regression now uses the observed 19223-token predecessor,
reproduces both old thresholds, verifies compaction/edit/real pytest with the
new threshold, and checks actual snapshot content and terminal cleanup.

Live-informed repair validation: full deterministic suite 466 passed, 3 opt-in
runtime tests skipped, 230 subtests passed in 645.43 seconds. The three native
runtime regressions passed separately in 34.00 seconds. Passing and failing
pytest assertions both executed with unchanged repository inventory.
