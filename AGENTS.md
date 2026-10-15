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
   system. Treat HotPin qualification as deferred optional work.

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

## Current verified status

- Last verified milestone (2026-10-15): LIVE_002_INDEPENDENTLY_CONFIRMED; Run ID
  `20261015T095036Z-a778f62c8873`, HEAD `92cd9e03148f4dd5fbe3bb532601c0a9d3f5d80d`.
  Real Qwen executor -> two dependent WorkUnits (`multiply` -> `scale`) -> intermediate
  verifications -> Nemotron critic (`CRITIC_CLEAN`) -> GPT-OSS 120B reviewer (`APPROVE`)
  -> fresh 15/15 final verification (ExistingAdditionTests, RequiredMultiplicationTests,
  RequiredScaleTests) -> supervisor/gateway cleanup verification -> validated trusted checkpoint
  `checkpoints/0000.json` -> authoritative WAL terminal seal `term-20261015T095036Z-a778f62c8873.json`.
  State COMPLETED, 213 validated WAL records.
- Prior milestone (2026-10-14): Live Run 001 (`20261014T091225Z-a23b9b7ed41c`), single
  WorkUnit, 10/10 tests, 136 WAL records, terminal acceptance seal.
- Public Run 003 (2026-10-10): earlier single-unit baseline.
- Offline CI status: 390/390 reviewed offline cases pass with 0 model calls under strict
  process, socket, and fixture guards (`scripts/ci_offline.py`).
- Known reporting discrepancy: `final-report.json` projection shows criteria as NOT_PROVEN
  (only AC1 in Live Run 001; AC1..AC3 in Live Run 002) due to legacy `check_completion()`
  serialization, while authoritative Store WAL and terminal seal establish acceptance.
  Original report files remain unmodified.
- Unverified: live error-driven recovery/replanning, live recovery/resume during active
  model inference, live TTL reload, and multi-step campaigns beyond 2 units.
- Next product priority: Manager review of release documentation, resolution of
  publication blockers, and reviewed feature branch PR for GitHub readiness.
