<img src="docs/triglav/assets/triglav-independent-v1.png" alt="TRIGLAV logo with three crowned heads" width="320">

# TRIGLAV

**Three Heads. One Verdict.**

**Experimental Public Preview** of a real local Python coding controller for native Windows.
TRIGLAV coordinates bounded code edits, deterministic verification, advisory
criticism, senior review, and durable checkpoints. Human operators retain semantic
review and publication authority.

The independent v1 logo and publication are approved by the project owner.
The prior reference-derived artwork remains excluded. See
[artwork provenance and usage](docs/triglav/assets/README.md).

## What works in this snapshot

The default `custom` executor uses a bounded JSON action protocol.
`run` executes a supervised coding workflow; `long-run`, `resume` and `status`
support durable state and recovery. `autonomous-run` and `autonomous-resume`
provide a bounded ManagerLoop. WorkUnit contracts, planning, scheduling, critic,
reviewer, verifier and checkpoint components are included as real source and tests.

| Component | Responsibility |
|---|---|
| Qwen / code executor | Proposes edits; its completion claim is untrusted |
| Nemotron / critic | Reports advisory findings; cannot authorize acceptance |
| GPT-OSS / senior reviewer | Reviews candidate code and verifier/critic evidence |
| Python controller | Owns scope, budgets, verification, lifecycle and checkpoint gates |
| Human operator | Reviews semantics, test suitability and promotion |

OpenCode is an opt-in executor. The Cline WorkUnit adapter is retained for its
historical route. Aider comparisons are not shipped as a supported runtime.

## Snapshot boundary

**The SafeQwen worker and launcher are not included. TQ-02R is historical,
external qualification evidence. Cloning this repository does not reproduce
TQ-02R.** Its reported 32-test result covers one bounded fixture, not this public
snapshot as a production-certified system.

No model weights, gateway binaries, llama.cpp/HotPin builds or third-party CLIs are
distributed. Provision them separately and review their licenses. No blanket
autonomous-engineering safety guarantee is made.

## Platform and prerequisites

- Native Windows; Python 3.11+, Git and PowerShell on PATH.
- A dedicated loopback llama-swap instance and compatible local model servers.
- Locally provisioned model weights and optional executor CLI installations.
- Sufficient RAM/VRAM for the chosen profiles; the historical large reviewer is
  resource intensive. No cross-platform or small-hardware qualification is claimed.

The controller uses the Python standard library. `requirements-dev.txt` lists
pytest for development; it is not required for the basic unittest example below.
No dependency, runtime or model is installed automatically.

## Setup and use

Obtain the public snapshot and work from its repository root:

```powershell
git clone https://github.com/MilJav11/triglav-harness.git
cd triglav-harness
```

Durable environment identity requires a committed controller checkout before runtime use.

1. Inspect [configuration and startup](RUNBOOK.md).
2. Provision the external runtimes and models yourself.
3. Edit [config/llama-swap.yaml](config/llama-swap.yaml) and
   [config/harness.json](config/harness.json) for your own executable, model and
   reviewer expert-file locations. Their existing generic absolute defaults are
   examples, not portable discovery.
4. Use a disposable, clean target Git repository with an initial commit.
   Target code and tests must be trusted.

Inspect the CLI without starting models:

```powershell
python -B harness.py --help
```

After provisioning and reviewing the configuration, these commands start/contact
the gateway; smoke performs real inference:

```powershell
.\scripts\start-swap.ps1
python harness.py health
python harness.py smoke --role code
```

Run a bounded task only when ready for local model execution:

```powershell
python harness.py run --repo '.\target-repo' --task 'Fix the requested behavior' --verify 'python -m unittest discover -v'
```

Add `--reviewer` for senior review; `--critic` requires the compatible reviewer
policy. Repeat `--verify` for independent checks. The controller leaves candidate
edits for inspection and does not commit or push them.

## Supported and experimental paths

The default custom executor is the primary CLI route. OpenCode 1.18.30 is a retained
opt-in integration, selected with `autonomous-run --executor opencode`; install
the CLI separately. Its success does not bypass controller verification or scope.
Cline is an external CLI dependency for the retained WorkUnit adapter, not the
default controller entry point. See [executor boundaries](OPENCODE_BOUNDARY.md).

SafeQwen distribution/portable integration, Aider adoption, smaller reviewers,
host isolation, and broad unattended or multi-project autonomy remain proposed
work. No experimental terminal banner is included.

## Testing and qualification

A narrow standard-library protocol suite uses mocked model replies:

```powershell
python -B -m unittest discover -s tests -p test_executor_protocol.py -v
```

The public-snapshot audit uses additional network/process guards. Do not run the entire
suite or smoke scripts blindly: retained tests include native process, local HTTP
server and optional CLI cases. See the [public validation summary](VALIDATION.md)
for executed scope, skipped work and the test-only identity shim used for the
uncommitted export.

Historical tests and recorded live workflows are separate from current offline
checks. The Manager reporting discrepancy—ALL_CRITERIA_PROVEN versus nested
AC1–AC3 NOT_PROVEN—is preserved. Model review and hashes do not establish semantic
correctness. See [qualification limits](docs/triglav/QUALIFICATION.md).

## Security and limitations

Use trusted repositories and tests. Verification can execute repository code.
Scope checks, closed protocols and Windows process ownership are not an OS sandbox.
A dedicated gateway is required because unload affects its resident models.
Concurrency, hostile-repository safety, all crash windows, long-horizon autonomy
and universal resource bounds are not established. Protect local logs and config;
no external evidence, private credentials or historic Git metadata is shipped here.

Read [safety and limitations](docs/triglav/SAFETY_AND_LIMITATIONS.md).

## Documentation and next steps

[Architecture](ARCHITECTURE.md) · [Runbook](RUNBOOK.md) ·
[Durable runs](DURABLE_RUNS.md) · [Process supervision](PROCESS_SUPERVISION.md) ·
[Recovery protocol](CONVERGENCE.md) · [Public documentation](docs/triglav/README.md) ·
[Roadmap](docs/triglav/ROADMAP.md).

Eligible original software is provided under the [MIT LICENSE](LICENSE),
copyright 2026 MilJav11. [Licensing and provenance](LICENSING.md) and the approved
[brand usage policy](BRAND_USAGE.md) keep the name, logo and identity separate.
