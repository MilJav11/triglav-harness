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

- Last verified milestone (2026-10-10): Public Full Harness 003 PASS; real Qwen
  executor -> ten frozen tests -> Nemotron critic -> GPT-OSS reviewer APPROVE
  -> fresh ten-test verification -> validated trusted checkpoint. State COMPLETED.
- Calls: Qwen 3, Nemotron 1, GPT-OSS 1. Critic returned one advisory finding;
  the independent reviewer approved. Only calculator.py changed.
- Approved current expert bytes, native ownership and 44 GiB startup gate passed;
  32 GiB hard working-set readback passed before inference and cached-role reuse.
  Owned model/Gateway cleanup passed. This is one bounded test, not provenance
  or optimization qualification; HotPin optimization remains deferred.
- Unverified: evidence-driven correction, recovery/resume, live TTL reload and
  multi-step long-horizon operation. Earlier run evidence remains preserved.
- Release candidate: guarded offline CI passed 209/209 cases with zero model
  calls; public calculator fixture and portable setup recipe prepared. This does
  not add live or long-horizon qualification.
- Next product priority: owner review of the complete GitHub release candidate
  and proposed commit contents; publication requires separate authorization.
