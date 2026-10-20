# TRIGLAV roadmap

Use [release status](../TRIGLAV_START_HERE.md) for the current baseline and
[architecture](../../ARCHITECTURE.md) for implemented behavior. This roadmap lists
qualification gaps, not a new development phase or execution authorization.

## Bounded repair: merged, live qualification outstanding

Phase 3.1 is merged in PR #8. Diagnostic evidence, eligibility checks, controller-owned
single-use grants, second-attempt dispatch and native verification are implemented.
The bound is **two logical attempts total (one retry)**; each worker invocation
is single-attempt. The ordinary operator CLI does not select these repair APIs.
Earlier full offline qualification covers the reviewed scope; the
[current GitHub CI status](../TRIGLAV_START_HERE.md#current-github-ci-status) remains separate.

Still needed: an explicitly authorized real-model experiment showing deterministic
failure -> evidence-driven second attempt -> fresh verification, followed by the
applicable independent review and acceptance gates. Existing successful first
attempts and Live 002 do not qualify live repair. Attempt 2 verification alone
must not promote trust. Preserve failures; no automatic rollback of ambiguous edits
or invented “trusted intermediate checkpoint” is part of this claim.

## Safe interruption and resume: unqualified live scope

Store recovery and resume mechanisms exist, but safe interruption/resume during
live model operation is not established. Initial proposed Phase 3.2 qualification
is limited to a verified boundary **between WorkUnits**, with repository/WAL/ownership
reconciliation and revalidation of prior units before further dispatch.

UNIT_VERIFIED is intermediate state, not an individual trusted checkpoint.
Do not promise Ctrl+C cleanup, resumption within an active WorkUnit or restoration
of an in-flight inference/token stream. The [historical design](PHASE3_DESIGN.md)
retains proposal detail; it is not current implementation authority.

## Longer campaigns and optional work

Live qualification remains bounded to small one- and two-unit fixtures.
Campaigns beyond two SafeQwen units, broader dependency graphs and real-world
unattended coding remain unqualified. The current opt-in SafeQwen multi-unit
contract is bounded to exactly two units.

Live TTL reload and further GPT-OSS 120B HotPin optimization/qualification are
optional deferred work and must not block the core coding/verification loop.
Smaller-reviewer evaluation and comparative studies of LongHorizon/Utopia remain
backlog items, not replacement architectures. Compatible model substitution still
requires configuration/adapters and evidence for the selected policy.

## Maintenance already merged

PR #9 adds the operator hardening and checkpoint-backed criteria projection;
PR #10 adds adaptive branding. Historical NOT_PROVEN reports remain unchanged;
a cached projection can still retain older values. Maintainer Windows Terminal
visual inspection passed; other-terminal visuals and current post-merge CI must
not be inferred from it. See [validation](../../VALIDATION.md) and
[branding](../TERMINAL_BRANDING.md).

[Qualification ledger](QUALIFICATION.md) · [Documentation index](README.md).
