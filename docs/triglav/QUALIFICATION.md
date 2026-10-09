# Historical qualification scope

PUBLIC PREVIEW. These are inherited sanitized descriptions of private historical
records. No original run, raw Store, private identifier or machine-local link is
distributed. No retained evidence was accessed for the current export review.

## What the recorded cases support

| Case | Worker route | Historical report | Limits |
|---|---|---|---|
| TQ-01 | Cline | Ledger fixture, 18 frozen tests, reviewer approval, final verifier and checkpoint | Prompt-only tool restrictions were insufficient confinement |
| TQ-02 | Cline | Required file deleted; verifier failed; no checkpoint | Negative evidence; a zero worker exit was not success |
| TQ-02R | External SafeQwen | 32 frozen tests, CRITIC_CLEAN, REVIEWER_APPROVED, final verifier and checkpoint | One bounded fixture; external source and deployment absent |

TQ-02R reportedly made one worker attempt with three completion requests:
read, exact edit, submit. Only the implementation file was writable; README and
tests were frozen. The closed worker registry contained read_files, edit_file,
submit. Downstream stage counts were one critic, one reviewer, one final verifier
and one checkpoint. Two unchanged reuse checks reused existing evidence.

**Cloning this repository does not reproduce TQ-02R.** The fixed external launcher,
worker, identity adapter, qualified model/runtime setup, frozen fixture and
retained evidence are not shipped. The snapshot is a public preview, not a
production-certified distribution.

## Reporting discrepancy

The historical top-level stop reason is ALL_CRITERIA_PROVEN. Nested AC1–AC3 retain
NOT_PROVEN, completion is null, and legacy counters do not represent the WorkUnit
stages. The source has distinct WorkUnit acceptance and legacy reporting paths.
That explains the difference without repairing or reconciling it.

The retained successful chain verdict was CHAIN_ACCEPTED_AWAITING_HUMAN_AUDIT.
A model approval or checkpoint does not establish separate human semantic approval.

## Negative semantic evidence

The earlier QUAL-004 case reportedly passed tests and reviews with an incorrect
application threshold. A later full-diff review also missed the defect.
QUAL-004R boundary tests rejected a related bad candidate. Preserve both outcomes;
do not generalize the successful fixture to arbitrary repositories or long-horizon
engineering.

## Current export checks

Current offline tests exercise selected controller protocols, packet bindings,
freshness, verification and disposable checkpoint behavior. They do not rerun
historical model qualification, validate omitted evidence or provision a live host.
See [public validation summary](../../VALIDATION.md).

[Evidence summary](EVIDENCE_SUMMARY.md#claim-map) ·
[Safety and limitations](SAFETY_AND_LIMITATIONS.md) · [Roadmap](ROADMAP.md).
