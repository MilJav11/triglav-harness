# Portable Windows setup baseline (Phase 2A)

**CLONE_READINESS: PARTIAL.** Source-only verification needs no models or private
helpers. The configuration examples below describe the public SafeQwen route;
they do not reproduce the privately qualified Phase 8B deployment. Stop at
preflight until the [reviewer resource-policy seam](#phase-2b-integration-seam)
is resolved and a new deployment is independently qualified.

## Source-only installation and verification

Use native Windows with 64-bit Python 3.11+ (3.11 is the CI baseline), Git and
PowerShell on PATH. No pip installation, virtual environment or pytest is needed
for the guarded checks. In a directory you own:

```powershell
python --version
git --version
git clone https://github.com/MilJav11/triglav-harness.git
cd triglav-harness
python -B harness.py --help
python -B scripts/ci_offline.py
git status --short
```

Expect exit 0 for help and CI, a final `PASS: 129 reviewed offline cases` line
with `model calls 0`, and an empty Git status. CI uses real local Git/Python
subprocesses and Windows Job Objects in disposable fixtures, including a sleeping
verifier that must be terminated. It disables Git hooks/signing/network protocols
for those fixtures; its fixture commits are not commits to the controller.
See [the guarded scope](CI.md). A process-restricted shell may block native Git
or Job Object operations: that failure is not a model dependency. Do not disable
the CI guard or controller ownership checks to obtain a pass.

`health`, `smoke`, ordinary runs and `scripts/start-swap.ps1` are outside this
source-only check. `-B` suppresses bytecode writes, not model execution.

## External components for a three-model deployment

These are public dependencies, separately obtained by the operator. Links were
checked on 10 October 2026; they are acquisition references, not a fully pinned
Phase 8B binary manifest. Record the actual revision, filenames, licenses and
SHA-256 checksums of your downloads before use. Do not silently substitute models
or assume the newest server accepts the historical flags.

| Component | Public source / acquisition | Required local asset |
|---|---|---|
| Python | [Python Windows releases](https://www.python.org/downloads/windows/) | Native x64 Python 3.11+ and standard library |
| Git | [Git for Windows](https://git-scm.com/install/windows) | `git` available to PowerShell and Python |
| llama-swap | [v260 release](https://github.com/mostlygeek/llama-swap/releases/tag/v260), the retained version | Windows executable, dedicated loopback gateway, operator-owned PID record |
| llama.cpp | [Upstream source and releases](https://github.com/ggml-org/llama.cpp) | Compatible Windows `llama-server.exe` plus its matching DLLs/backend files for Qwen and Nemotron |
| GPT-OSS HotPin runtime | [HotPin upstream](https://github.com/LozzKappa/hotpin-llm) | Separate patched llama.cpp server and matching DLLs; model-compatible Top4 expert selection file |
| Qwen code model | [Published Qwen GGUFs](https://huggingface.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF) | `Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf` |
| Nemotron critic model | [Published Nemotron GGUFs](https://huggingface.co/aj9o9/nvidia-nemotron-3.5-lightning-30b) | `NVIDIA-Nemotron-3.5-Lightning-30B-A3B-Q4_K_M.gguf` |
| GPT-OSS reviewer model | [Published GPT-OSS GGUFs](https://huggingface.co/unsloth/gpt-oss-120b-GGUF) | `gpt-oss-120b-Q4_K_M-00001-of-00002.gguf` **and** `gpt-oss-120b-Q4_K_M-00002-of-00002.gguf`, together |

GGUF repositories are publishers' download locations, not proof of the privately
qualified files' identity. Preserve shard names and obtain every shard if your
chosen download is split. Follow the chosen runtime's CPU/GPU/backend requirements.
No universal RAM/VRAM minimum or performance guarantee was established.

HotPin is public source/patch tooling, not a bundled TRIGLAV executable. Its
upstream build instructions name a llama.cpp base revision; building it on Windows
requires CMake and a supported C++ toolchain separately. The exact privately
qualified Windows build and Top4 file are not distributed by TRIGLAV. Upstream's
example hot file is not automatically the required Top4 selection for these
weights. A stock server or merely setting `LLAMA_HOT_EXPERTS` does not establish
the qualified policy. See [third-party licensing](../LICENSING.md).

OpenCode, Cline and Aider are not prerequisites for this SafeQwen route. The
private Stage 2 operator/watchdog, qualification broker/heartbeat/resource-check
helpers, frozen fixture/oracle, deployment manifests and retained telemetry/Store
are qualification-only assets, not downloadable TRIGLAV dependencies. The public
`supervision_worker.py`, Supervisor and terminal progress remain available;
they do not supply the private operator's whole-session resource policy. Do not
copy private helpers, sessions, raw logs, prompts or evidence into the checkout.

## Prepare operator-local configuration

From the controller root, copy the examples to already ignored locations:

```powershell
New-Item -ItemType Directory -Force runtime | Out-Null
Copy-Item config/safeqwen-three-model.example.json config/safeqwen.local.json
Copy-Item config/safeqwen-policy.example.json config/safeqwen-policy.local.json
Copy-Item config/llama-swap.safeqwen.example.yaml runtime/llama-swap.yaml
```

The JSON uses paths relative to its own `config/` directory. Put separately
provisioned assets under ignored `runtime/`, or edit those paths to an external
local directory. The gateway YAML uses the fictional `C:/TRIGLAV-runtime` root:
replace **every** occurrence with your actual absolute directory. JSON and YAML
must identify the same server/model/config files. No variable/path expansion or
automatic discovery is provided by the SafeQwen identity adapter.

Keep exactly the enabled roles in `safe_qwen_runtime`: all three entries require
global `--critic` and command-local `--reviewer`. The older
`safeqwen.example.json` declares only the code runtime; its extra routing aliases
do not enable critic/reviewer. All role entries share one gateway YAML. Each YAML
alias must occur once with two-space indentation, `cmd: >-`, and the entire quoted
executable / quoted `--model` command on the following **single line**. These
restrictions come from `safe_executor_binding.model_starting`, not a general YAML
parser. Keep loopback binding, no preload and sequential loading.

Do not use `scripts/start-swap.ps1` for these examples: it renders the legacy
`config/harness.json` / `config/llama-swap.yaml` profile deployment. SafeQwen
explicitly refuses nonempty `review_profiles`. Its dedicated gateway is
operator-started; `safe_qwen_gateway.pid_file` must contain the decimal PID of
that actual gateway, never a guessed/stale PID. The operator owns final gateway
shutdown; the public controller supervises its model children and unloads them.
No gateway is started by copying these files or by dry-run.

The sample RAM thresholds (code 16, critic 30, review/global 44 GiB free, active
6 GiB) are inherited policy examples, not qualified limits for a new workstation.
Without `review_profiles`, `Gateway._switch` reads the review startup threshold
from top-level `min_available_ram_gb`; `reviewer.min_available_ram_gb` would not
enforce it. **There is no 32 GiB hard cap in this configuration.** Do not lower
guards to force startup or represent free-RAM checks as a process memory ceiling.

Adapt the exact-file policy to a separate clean committed disposable Git target
with trusted tests: readable/writable files must exist, be UTF-8, and fit the
65,536-byte policy limit. Keep tests frozen. No production repository, credential
or operator configuration belongs in that scope. After provisioning the declared
local files and a valid operator PID record, this command is preflight only:

```powershell
python -B harness.py --config config/safeqwen.local.json --critic autonomous-run `
  --executor safe_qwen --safeqwen-policy config/safeqwen-policy.local.json `
  --repo ../disposable-calculator --task "Implement multiply in calculator.py" `
  --verify '["python", "-m", "unittest", "test_calculator.py"]' `
  --acceptance "multiply returns the correct product" `
  --allow calculator.py --forbid test_calculator.py `
  --reviewer --review-policy three-model --dry-run --quiet
```

Dry-run requires declared local resources but neither constructs a Gateway nor
verifies a live PID, YAML command identity, model load or actual resource cap.
Its success is configuration/scope preflight, not live readiness. Removing
`--dry-run` invokes models. Live setup/qualification is outside Phase 2A.
See [SafeQwen exit codes, recovery and boundaries](SAFEQWEN.md#actual-cli).

## Phase 2B integration seam

The missing seam is **explicit SafeQwen reviewer resource-policy binding**:

1. `terminal_run.configure_worker` rejects nonempty `review_profiles`.
2. `harness.Gateway._switch` checks the owned review child's HotPin environment
   and calls `winprocess.limit_working_set` only when a selected legacy profile
   exists. Profile-free SafeQwen skips both operations.
3. `safe_executor_binding.extend_capture` pins the declared binary and gateway
   YAML, but supplies no dedicated expert-file identity or enforced reviewer cap.
   A YAML environment string pins the filename text, not the expert-file bytes.

The existing public route already wires SafeQwen through Manager WorkUnits,
deterministic verification, Nemotron, GPT-OSS, fresh final verification and trusted
checkpoint gates. The critic parser supports both its WorkUnit and legacy wire
schema variants; it is not a missing three-model adapter. Another launcher or a
private watchdog copy is unnecessary to address the resource-policy seam.

**Phase 2B requires production Python integration changes** to reproduce the
recorded checked Top4 / 32 GiB policy. Add an explicit bounded reviewer policy for
the dedicated SafeQwen route, bind/hash its expert file in configuration and
environment identity/resume checks, and reuse the existing Supervisor child
identity, `hotpin_environment` and `limit_working_set` readback before review
requests. Fail closed on inheritance/cap/identity failure and retain existing
unload/cleanup authority. Add offline regressions for mismatch, failed cap,
runtime drift and cleanup; separately authorize live qualification on a fresh
public disposable fixture. Do not bypass the profile rejection or claim equivalent
safety from startup flags. No such Python changes are made in Phase 2A.

Independent three-model reproduction also needs provisioned compatible binaries,
all model shards, a matching Top4 file, owned gateway/PID configuration, measured
host capacity, and new live lifecycle/resource/failure-path validation. The frozen
private Phase 8B fixture/oracle and evidence remain unavailable; a new public
fixture can qualify a new deployment but cannot reproduce that historical run.
The recorded Manager summary versus nested acceptance discrepancy remains as
documented in [qualification limits](triglav/QUALIFICATION.md#reporting-discrepancy).

## Phase 2A offline validation (10 October 2026)

A fresh isolated local clone of Phase 1 commit
`68455ec2c9bebebdc91673ad885f61455c6f5e0f` was created without shared object
hardlinks. The source-only Quick Start passed on native Windows with Python
3.12.10 and Git 2.55.0.windows.5: CLI help exit 0, 73 Python files parsed,
**129/129 guarded offline cases passed**, 84 real controller HEAD probes and
**model calls 0**. No dependency was installed and no private helper was used.
The restricted-shell attempt stalled at the native verifier case and was stopped;
the same unmodified guarded runner passed with native-process access. This is
not a new Python 3.11 run or a live model qualification.

The new three-role JSON and gateway command layout were separately checked with
inert temporary runtime files and the existing disposable calculator fixture.
The documented three-role CLI dry-run passed with Gateway construction blocked
and socket guards enabled; alias, binary/model path agreement and local document
links passed. No server executable, model or gateway YAML validator was run.
Dry-run does not establish live ownership, runtime flag compatibility or HotPin
resource enforcement. The private frozen Phase 8B fixture was not accessed.

Existing Python/PowerShell source, tests, startup scripts and active configuration
remain unchanged. Only documentation and new configuration examples are in this
Phase 2A change. No repository commit, push, tag or publication was performed.
