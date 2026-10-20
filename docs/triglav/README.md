# TRIGLAV documentation index

Start with the [README](../../README.md). The [release locator](../TRIGLAV_START_HERE.md)
contains the dated baseline and qualification boundaries; this index links to
details without duplicating project state.

## Current guides

- [PowerShell operator](../OPERATOR_RUNBOOK.md): canonical tracked wrapper, prerequisites, DryRun/session writes, live startup and exit codes.
- [Portable Windows setup](../WINDOWS_SETUP.md): external assets, local configuration and disposable calculator demo.
- [Architecture](../../ARCHITECTURE.md): current routes, deterministic trust, checkpoints versus terminal acceptance, and merged repair limits.
- [Offline CI](../CI.md): 390 guarded / 1,296 mandatory qualification IDs, separate branding step and evidence collection; [current GitHub CI status](../TRIGLAV_START_HERE.md#current-github-ci-status).
- [Terminal branding](../TERMINAL_BRANDING.md): display controls and maintainer Windows Terminal visual PASS; no cross-terminal visual qualification.
- [Validation history](../../VALIDATION.md): dated offline and live scope, including current reporting semantics.
- [Qualification ledger](QUALIFICATION.md): preserved sanitized historical live milestones.
- [Roadmap](ROADMAP.md): remaining live repair, resume and longer-campaign qualification.
- [Project contract](../../AGENTS.md): engineering and preservation rules.

## Scoped technical and historical references

- [SafeQwen integration](../SAFEQWEN.md) and [HotPin policy](../HOTPIN_POLICY.md): tool/identity/resource contracts; their Phase 8 and 129/209-case validation sections are historical, not current CI totals.
- [SafeQwen migration identities](../SAFEQWEN_SOURCE.md): historical source provenance, not a current runtime manifest.
- [Durable storage/recovery](../../DURABLE_RUNS.md) and [process supervision](../../PROCESS_SUPERVISION.md): route-specific persistence and ownership details.
- [Legacy Windows/custom runbook](../../RUNBOOK.md), [recovery notes](../../CONVERGENCE.md) and [OpenCode boundary](../../OPENCODE_BOUNDARY.md): earlier routes and dated investigations; not substitutes for the current operator guide.
- [Historical external architecture](ARCHITECTURE.md), [evidence claim map](EVIDENCE_SUMMARY.md#claim-map) and [safety background](SAFETY_AND_LIMITATIONS.md): historical deployment context; current reporting/repair status is in the guides above.
- [Historical Phase 3 design](PHASE3_DESIGN.md): original proposal, with current-status pointers; not a new implementation checklist.

## Local evidence and external dependencies

The controller stores private run bundles under `runs/<run-id>/`. events.jsonl is
the authoritative WAL; state.json is a cache and final-report.json a projection.
Review artifacts use milestones/, final checks final_verification/, checkpoints
checkpoints/, and terminal-enabled runs acceptance/. Legacy rounds use rounds/.
A checkpoint is not a terminal seal. Keep full referenced evidence locally;
do not publish raw prompts, sessions, credentials, model paths or archive inventories.

The repository includes standard-library Python controller/tests, configuration
examples, the operator script and terminal artwork. Compatible llama-swap,
llama.cpp/optional HotPin binaries, licensed model weights and approved expert
assets are separately provisioned. The wrapper contains installation-specific
paths; portable setup uses local JSON/YAML files. New hosts require their own
validation. Optional OpenCode/Cline adapters are separate executor routes.

[.gitignore](../../.gitignore) excludes specified runtime/run/configuration paths;
it is not a blanket protection for all local assets. Review publication scope.
Original qualification reports and source provenance remain historical evidence.

[Licensing](../../LICENSING.md) · [MIT license](../../LICENSE) ·
[Brand policy](../../BRAND_USAGE.md) · [Artwork provenance](assets/README.md).
