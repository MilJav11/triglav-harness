# TRIGLAV public documentation

**Three Heads. One Verdict.**

This Experimental Public Preview documents the real local Windows controller and distinguishes it
from the historical external SafeQwen deployment. The controller, tests and
configuration templates are included. Local models, gateways, binaries and
third-party CLIs must be provisioned separately.

The [SafeQwen integration](../SAFEQWEN.md) passed 129/129 guarded offline cases.
[Phase 8B Stage 2](QUALIFICATION.md#phase-8b-stage-2) completed one bounded
three-model fixture qualification on a privately provisioned deployment.
Clone readiness is **PARTIAL**; cloning does not reproduce that full chain or
historical TQ-02R. Neither result establishes general autonomous coding.

- [Current SafeQwen integration and CLI](../SAFEQWEN.md)
- [Original/migrated SafeQwen source identities](../SAFEQWEN_SOURCE.md)
- [Current snapshot architecture](../../ARCHITECTURE.md)
- [Historical external route architecture](ARCHITECTURE.md)
- [Current qualification summary and historical outcomes](QUALIFICATION.md)
- [Evidence interpretation and claim map](EVIDENCE_SUMMARY.md#claim-map)
- [Safety and limitations](SAFETY_AND_LIMITATIONS.md)
- [Roadmap](ROADMAP.md)
- [Configuration and actual CLI](../../RUNBOOK.md)
- [Current validation scope](../../VALIDATION.md)
- [Licensing and provenance](../../LICENSING.md)
- [MIT software license](../../LICENSE)
- [Brand usage policy](../../BRAND_USAGE.md)
- [Approved independent artwork](assets/README.md)

No private reports, raw runs, chat identifiers, reference-derived art or
experimental terminal branding are distributed.

## Architecture and operation

Start with the [current source architecture](../../ARCHITECTURE.md), including its
Mermaid trust flow, then the [runbook](../../RUNBOOK.md),
[SafeQwen CLI and policy](../SAFEQWEN.md),
[durable storage/recovery](../../DURABLE_RUNS.md#storage-and-trust) and
[process supervision](../../PROCESS_SUPERVISION.md). The
[historical external architecture](ARCHITECTURE.md) retains accurate historical
Mermaid diagrams; it describes the earlier launcher/binding route.
For model-free usage, use the [source-only Quick Start](../../README.md#quick-start)
and [guarded CI scope](../CI.md).

## Local Store and checkpoint records

The public controller normally stores durable records under `runs/<run-id>/`
in its controller checkout. `state.json` is the cache, `events.jsonl` the ledger,
`rounds/` holds legacy round verification/review artifacts. WorkUnit milestone
state and evidence references are recorded in `state.json` and `events.jsonl`;
critic and senior reviewer result artifacts are under `milestones/`, and
fresh final verification evidence is under `final_verification/`.
`checkpoints/` holds checkpoints, and `final-report.json` is the report. See the
[storage protocol](../../DURABLE_RUNS.md#storage-and-trust).
These are operator-local records, not public documentation. Private qualification
tooling also preserves session records and Store copies outside the public source
tree; no private location or archive is published here.

## Git exclusions

[.gitignore](../../.gitignore) excludes `runs/`, `tools/llama-swap/`, `/runtime/`,
the operator files `/config/safeqwen.local.json` and
`/config/safeqwen-policy.local.json`, `.venv/`, `.pytest_cache/`, `__pycache__/`
and `*.pyc`. Model weights, native binaries, private sessions, credentials,
raw prompts and Store archives are not distributed. Git ignore rules cover only
their listed paths; keep other operator assets outside the checkout or explicitly
excluded, and inspect changes before publication. Public configuration examples
and sanitized qualification summaries are tracked.

## Dependencies and missing integration assets

| Publicly available source/guidance | Separately provisioned or private assets |
|---|---|
| Standard-library Python controller, tests, SafeQwen worker/launcher and identity binding | Native Windows, Python 3.11+, Git and PowerShell installation; pytest only for optional development use |
| Gateway configuration templates and startup scripts | Compatible llama-swap and llama.cpp/HotPin binaries, licensed Qwen/Nemotron/GPT-OSS weights, reviewer expert files and resource capacity |
| SafeQwen configuration/policy examples | Exact local runtime commands, paths, ownership/PID records, role configuration and operator policy for a new host |
| Retained optional OpenCode/Cline adapters | Separately installed third-party CLIs and their own configuration/licenses |
| Sanitized Phase 8B Stage 2 qualification summary | Private operator/watchdog, broker, heartbeat, lifecycle/resource-check helpers, frozen qualification fixture/oracle, deployment manifests, session telemetry and Store evidence |

The private integration assets are not a public setup kit. Source-only checks
are reproducible within their documented Windows scope; full-chain clean-clone
reproduction remains unproven. New deployment validation and broader fixture,
failure-path and autonomy qualification remain [roadmap work](ROADMAP.md).
