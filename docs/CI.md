# Offline CI scope

The [Offline CI workflow](../.github/workflows/offline-ci.yml) runs on
GitHub-hosted Windows 2025 with Python 3.11, the documented minimum supported
version. It runs for pull requests targeting main and pushes to main.

Use the same entry point locally from a committed Windows checkout:

```powershell
python -B scripts/ci_offline.py
```

No workstation dependency installation is needed. Python's standard library and
Git are sufficient. The workflow uses official checkout v7.0.1 and setup-python
v7.0.0 actions pinned to their full verified commit SHAs. Python provisioning is
limited to the ephemeral GitHub runner; no local model/gateway/CLI is installed.

## Reviewed checks

- Parse all repository Python source with ast.parse.
- Thirty-five isolated tests of the CI guard itself (including denial of
  unrestricted discovery, arbitrary test_sub variants, checkout verifiers,
  unregistered processes, unrelated temporary fixture injection, spoofed missing
  modules, tampered fixture content, unapproved module resolution, runner replacement,
  package shadowing, cached bytecode execution, copied approved fixture profiles,
  fake git metadata without token, unrelated Repository initialization without token,
  trusted fixture registration, content mutation after registration, adoption rejection
  of pre-existing repositories before helper execution, preservation of existing content
  when adoption is refused, failed construction non-registration, and unapproved
  candidate mutations).
- Eight executor-protocol tests and fourteen planner-protocol tests.
- Thirty-nine reviewed SafeQwen development/worker tests, with scripted responses.
- Forty SafeQwen integration/ownership/recovery tests, plus ten milestone timeout regressions.
- Twenty-six HotPin policy contract, expert identity, resume, compatibility and native-preflight regressions using disposable fake assets.
- Fifty-four native enforcement cases with fake processes/Windows handles and scripted responses.
- Twenty-five Supervisor lifecycle, PID tracking, and job containment tests.
- Thirty-nine durable store tests qualifying terminal acceptance primitives, fault regressions, and Codex-identified invariant guarantees.
- Seventy reviewer and terminal lifecycle regressions (`test_reviewer_wave6`), including forty-one M0-B controller authority / WAL publication guard tests, eleven M0-C terminal lifecycle acceptance tests, four focused corrections, six regression repairs, and eight core wave-6 verifier/checkpoint regressions.
- Seventeen SafeQwen multi-WorkUnit integration and negative regression tests (`test_safe_qwen_multi_unit` and `test_safe_qwen_multi_unit_prod`).
- Eleven retained Manager, recovery, checkpoint, reviewer, and resume qualification regressions (390 reviewed offline cases in total).
- Import and capture CLI --help and SafeQwen dry-run output.

The protocol tests use mocked or scripted model replies. Planner correction cases
create disposable Git repositories and run the known addition/calculator unittests and Git whitespace check through
the real Python supervision broker. Its Windows process ownership is exercised;
there is no gateway, model server or arbitrary verifier invocation. The only
shell allowance is the exact System32 cmd /c ver query used by Python 3.11
for read-only Windows version metadata.
Imported helper modules do not imply that their other test cases are executed.
The rest of the existing suite remains outside reviewed scope; importing helper
modules does not execute their unrelated test cases. The eight selected cases
from test_reviewer_wave6 and eleven selected Manager cases are listed explicitly
in scripts/ci_offline.py.

The timeout regressions include a disposable sleeping verifier executed through
the real Windows supervision broker; its forced exit and drained ownership records
are checked. No model process is involved.

## Guard boundary

The runner permits the local hostname query used by platform.uname and denies
all other socket operations, unmocked Gateway request/switch/unload/running,
other shell commands, alternative process launch, unknown executables and unreviewed process commands.
Git operations are restricted to read-only checkout queries and disposable-fixture
operations; networking verbs and mutations of the controller checkout are denied.

Verification authorization binds strictly to both runner-controlled authorized fixture
provenance and exact approved test file content hashes (`test_add`, `test_sub`, and
`test_calculator.py`). Unrelated temporary fixtures cannot qualify merely by
providing matching filenames, copying an approved content profile, requesting `git init`/`commit`,
or creating a `.git` directory. Fixture registration requires a private capability token
held only by runner-wrapped trusted test constructors (`test_manager.git_fixture`,
`test_reviewer_wave6.setup_test_fixture`, and `test_work_units_wave2.fixture`) along with matching
an independently reviewed fixture structure and content profile upon creation. The token controls
registration strictly through the intended Python API; it does not provide isolation against
arbitrary code executing within the same interpreter process, where object references can be
inspected in the runtime. Trusted constructors fail closed if an expected repository path already
exists before invocation, preventing adoption of pre-existing or copied repositories. Arbitrary
`Repository` initialization or fixture self-registration without the private token fails closed,
and the complete executable dependency state is re-validated at verifier launch.

At verifier launch, the full executable import chain is validated against reviewed expectations:
- **Test runner**: stdlib `-m unittest` is preserved; local runner replacement (`unittest.py`,
  `unittest/`, `sitecustomize.py`, `usercustomize.py`, `*.pth`) is strictly denied.
- **Test module**: `test_add.py`, `test_sub.py`, and `test_calculator.py` must match reviewed
  content hashes; package shadowing directories (`test_add/`, `test_calculator/`, etc.) are prohibited.
- **Candidate modules**: imported candidate code (`calc.py`, `calculator.py`) must match a
  finite reviewed set of legitimate states (baseline, deliberately failing attempts, repaired,
  or completed). Unapproved candidate mutations fail closed before child execution.
- **Resolution and bytecode**: package shadowing directories (`calc/`, `calculator/`), cached
  bytecode (`__pycache__/`, `*.pyc`), and unexpected files in the fixture are strictly rejected.
- **Negative tests**: the missing-module verifier requires that no file or directory matching
  `test_missing_module*` exists anywhere in the fixture before execution proceeds.

Because Python audit hooks do not propagate to child processes, security is
enforced by ensuring that only pre-verified, reviewed test sources can ever be
spawned. While pre-launch synchronous verification ensures all sources and dependencies
match reviewed expectations, a writable fixture directory and child process do not
constitute an OS-level security sandbox: atomic, race-free time-of-check/time-of-use
(TOCTOU) guarantees cannot be made against concurrent in-process modification without
kernel-level containerization or execute-from-file-descriptor primitives. Similarly,
pre-construction path existence checks prevent accidental adoption through the intended
Python API, but do not provide atomic, race-free filesystem isolation against concurrent
hostile file creation within the same shared temporary directory.

Git hooks, signing, global/system configuration and network protocols are
disabled for tests. No controller runtime implementation or assertion is
replaced.

Controller HEAD probes use actual Git on the committed checkout. Unlike the earlier
uncommitted export audit, there is no HEAD identity shim.

The Python audit hook does not propagate into subprocesses. The broker path,
fixture directory and verifier command are checked before execution, and their
included code has been reviewed. These guards catch accidental regression; they
are not an OS sandbox for deliberately malicious pull-request code.

## Workflow security and limits

The workflow has contents: read only, a ten-minute timeout and no repository secrets,
credential persistence, deployment, publication, artifact upload or dependency
cache. It uses pull_request rather than pull_request_target, and executes the
event revision on an ephemeral hosted runner. It does not grant write permissions
or consume private evidence. Fork-run approval follows GitHub's repository policy.

Success covers this bounded offline scope, not the full test suite, live platform
setup, model quality, live SafeQwen deployment, TQ-02R reproduction or production
qualification. Failure output remains visible in the Actions log; no raw local
audit report or retained private evidence is uploaded.

[Validation history](../VALIDATION.md) Â·
[Safety limits](triglav/SAFETY_AND_LIMITATIONS.md).

See [SafeQwen tests and limits](SAFEQWEN.md#offline-checks-and-historical-scope).
