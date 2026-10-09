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
- Ten isolated tests of the CI guard itself.
- Eight executor-protocol tests and fourteen planner-protocol tests.
- Thirty-nine reviewed SafeQwen development/worker tests, with scripted responses.
- Forty SafeQwen integration/ownership/recovery tests and eight selected
  existing final-verification/checkpoint regressions: 119 cases in total.
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
from test_reviewer_wave6 are listed explicitly in scripts/ci_offline.py.

## Guard boundary

The runner permits the local hostname query used by platform.uname and denies
all other socket operations, unmocked Gateway request/switch/unload/running,
other shell commands, alternative process launch, unknown executables and unreviewed process commands.
Git operations are restricted to read-only checkout queries and disposable-fixture
operations; networking verbs and mutations of the controller checkout are denied.
Verification is restricted to the reviewed fixtures' addition/calculator unittests and
git diff --check. Git hooks,
signing, global/system configuration and network protocols are disabled for tests.
No controller runtime implementation or assertion is replaced.

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
