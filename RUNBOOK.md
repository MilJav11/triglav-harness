# TRIGLAV public Windows runbook

Use native Windows, Python 3.11+, Git and PowerShell. Run commands from the
repository root. This is configuration guidance, not proof that the public
snapshot reproduces a qualified host.

## Provision external dependencies

Obtain llama-swap, compatible llama.cpp/HotPin builds, required model weights and
any optional executor CLI from their upstream projects. Review their licenses,
version compatibility and checksums independently before execution. They are not
included in this snapshot. The historical configuration references llama-swap
v260, OpenCode 1.18.30 and a Cline 3.0.68 WorkUnit route; these are retained version
choices, not a claim about current upstream releases.

The startup script expects the llama-swap executable at
`tools/llama-swap/bin/llama-swap.exe`. That runtime directory is ignored by Git.

## Configure before startup

Edit [config/llama-swap.yaml](config/llama-swap.yaml) for each server executable and
model path. Retain the intended loopback binding and sequential role lifecycle.
Edit [config/harness.json](config/harness.json) for reviewer expert-file locations,
timeouts, role metadata and the chosen resource profile.

The existing absolute defaults in runtime code/templates are generic installation
conventions. They contain no user home path, but are not portable. Do not expect
automatic discovery of your model files. The Cline adapter supports an executable
override under `cline.executable`; OpenCode uses `opencode.executable`.
Do not change controller security or verification policy merely to make setup pass.

The startup renderer substitutes the selected expert file into a generated
reviewer definition. `review-safe` uses Top4 and a 32 GiB working-set ceiling;
`review-performance` uses Top6 without that ceiling. Their names are policy
choices, not universal performance guarantees. The historical large reviewer
needs substantial system RAM.

## Inspect the CLI

```powershell
python -B harness.py --help
```

This parser path does not start a gateway or model. There is no --version option.
Errors and progress use stderr; result formats differ by command. --quiet suppresses
progress, not structured result output.

## Start, health and smoke

These commands are live operations. Provision and review configuration first.
Use a dedicated loopback gateway and only one controller at a time.

```powershell
.\scripts\start-swap.ps1
python harness.py health
python harness.py smoke --role code
```

Startup launches the gateway. Health queries it. Smoke loads/calls/unloads a model.
To explicitly test the reviewer, use `smoke --role review`; do not load another
large model concurrently. These operations were not executed during export review.

## Run a trusted target

The controller and target each need a committed Git baseline for their respective
identity checks. Use a committed public controller checkout and a clean target
repository with an initial commit before runtime use.

Use a separate clean disposable target repository containing no sensitive data.
Tests can execute arbitrary target code; path checks are not host isolation.

```powershell
python harness.py run --repo '.\target-repo' --task 'Describe the requested change' --verify 'python -m unittest discover -v'
```

Repeat --verify as needed, or use a JSON argv array when quoting is complex.
Add --reviewer for GPT-OSS review. --critic is advisory and requires a compatible
reviewer policy. The controller leaves edits for human inspection.

## Durable state and recovery

```powershell
python harness.py long-run --repo '.\target-repo' --task 'Fix the boundary case' --verify 'python -m unittest discover -v' --acceptance 'Boundary tests pass'
python harness.py status --run-id YOUR_RUN_ID
python harness.py status --run-id YOUR_RUN_ID --json
python harness.py resume --run-id YOUR_RUN_ID
```

Use the actual newly created run identifier, not a historical private identifier.
Environment drift and unfinished work require explicit reviewed revalidation;
do not reset, delete or repin old state to force resume.
See [durable run rules](DURABLE_RUNS.md) and [supervision](PROCESS_SUPERVISION.md).

## Bounded autonomous and optional executors

```powershell
python harness.py autonomous-run --repo '.\target-repo' --task 'Fix the calculation' --verify '["python","-m","unittest","-v"]' --acceptance 'Calculation tests pass' --allow calc.py
```

OpenCode is opt-in through --executor opencode. Its candidate output remains subject
to controller scope, verifier, review and checkpoint gates. Cline is a retained
WorkUnit adapter; no SafeQwen launcher or worker is included.
See [executor boundaries](OPENCODE_BOUNDARY.md).

## Stop and inspect

```powershell
.\scripts\stop-swap.ps1
```

Inspect locally generated logs and the exact owned process state. Keep logs,
model outputs, configurations and evidence private unless independently reviewed.
No original local run evidence or historic process measurement is included here.

For offline checks, see [VALIDATION.md](VALIDATION.md). Do not treat smoke or
measurement scripts as model-free unit tests.
