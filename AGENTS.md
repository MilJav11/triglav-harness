# TRIGLAV Project Contract

## Mission

Deliver the simplest functional, efficient, reproducible, locally running
long-horizon autonomous coding harness inspired by AMAP-ML/LongHorizon-Harness.

## Priorities (in order)

1. Make the plan -> execute -> deterministic verify -> evidence-based recovery
   -> trusted checkpoint loop work.
2. Replace compatible local AI models through configuration and minimal adapters,
   not architectural rewrites.
3. Make GitHub onboarding, offline scripted tests and a reproducible local live
   demonstration easy.
4. Optional optimizations, including GPT-OSS 120B HotPin, must never block the core
   system. Treat further HotPin optimization/qualification as deferred optional work.

## Engineering rules

- Reuse existing architecture and code; prefer the smallest working change.
- Add no speculative frameworks, services or abstractions.
- Do no extended research without a demonstrated blocker.
- Never bypass deterministic verification, scope restrictions or checkpoint
  requirements. Choose a supported core execution path without weakening gates.
- Never claim mock/offline success proves live behavior.
- Do not download, perform destructive operations, commit, push or run live
  inference without appropriate authorization for the task.
- Preserve all reviewed Phase 2B.3 work.

## Mandatory for every task

1. Read this AGENTS.md before starting.
2. Check the current repository, branch, HEAD and Git status/diff; preserve
   existing work.
3. Identify the smallest step delivering real user value.
4. Define a measurable acceptance test before making changes.
5. Stop after success or a genuine blocker.
6. Report evidence and exactly one next action.

## MVP acceptance

A supported local model completes a bounded coding task in a disposable Git
repository, with deterministic tests, evidence-driven correction and a trusted
checkpoint only when required approval gates pass. A public offline scripted
demonstration must also be reproducible without models.

## Release status and evidence boundaries

As of 2026-10-20, the authoritative release checkout is `triglav-release`,
based on `MilJav11/triglav-harness` main at `c6a90fd`. Verify actual Git state;
documentation branches may sit on that baseline. Use [README](README.md) as the
entry point and [release status](docs/TRIGLAV_START_HERE.md) as the dated locator.

- Merged: Phase 3.1 bounded repair (PR #8), operator hardening and checkpoint-backed
  criteria reporting (PR #9), adaptive terminal branding (PR #10).
- See [current GitHub CI status](docs/TRIGLAV_START_HERE.md#current-github-ci-status).
  Configured scope is 390 guarded cases and 1,296 total mandatory
  qualification IDs, with a separate branding/operator step. Earlier offline
  qualification does not prove this revision or live model behavior; see [CI](docs/CI.md).
- Strongest independently confirmed live terminal milestone: Live 002 (2026-10-15),
  two dependent units, real Qwen/Nemotron/GPT-OSS, 15/15 final tests and 213 validated
  WAL records. Keep full historical identities and prior results in the
  [qualification ledger](docs/triglav/QUALIFICATION.md).
- `UNIT_VERIFIED` / `MILESTONE_READY` are intermediate. A trusted checkpoint is
  distinct from separately enabled WAL terminal acceptance; historical B2 has a
  checkpoint, not a terminal seal. Model approval never overrides required gates.
- PR #9 projects checkpoint-backed verifier evidence into criteria. Original
  historical NOT_PROVEN reports remain unchanged; cached projections can retain
  the old discrepancy. Do not regenerate historical evidence to update documentation.
- Canonical operator: tracked `scripts/Invoke-Triglav.ps1`. Its standalone installed
  copy differs. DryRun writes session configuration but starts no models; SafeQwen
  edits existing files only. See [operator guide](docs/OPERATOR_RUNBOOK.md).
- Maintainer visual inspection: **Windows Terminal PASS** for portrait, color and
  alignment. No cross-terminal visual qualification is claimed.
- Unqualified: real-model error-driven repair, live interruption/resume, TTL reload,
  campaigns beyond two SafeQwen units and production autonomy. Merged repair APIs
  are separate from the ordinary one-attempt operator path.

Preserve existing untracked `.github/diagnostics/` and
`.github/workflows/f3f-shard8.yml`. Keep local inventory, private paths and raw
evidence outside public documentation. Publishing, model runs and cleanup require
authorization for the specific task.
