# Public qualification summary

PUBLIC PREVIEW. This sanitized summary reflects the verified readiness assessment
and retained final qualification report. No original run, raw Store, private
identifier, prompt or machine-local link is distributed. No models were run for
this documentation update.

## Phase 8B Stage 2

**PHASE_8B_STAGE2_PASS**, completed 10 October 2026. The verified public source
baseline is `1a3167830f8d1fdf79dba255346784f57793a3ce` (111 tracked files;
129/129 guarded offline CI cases passed). The live chain used that source with
private operator tooling and locally provisioned runtimes.

| Check | Verified recorded result |
|---|---|
| Qwen | One bounded worker attempt; three requests/tool actions; only `calculator.py` changed; frozen README/tests unchanged; **8 FAIL / 4 PASS → 12/12 PASS** |
| Nemotron | One request; **CRITIC_FINDINGS**, with one preserved **MINOR** `behavioral_change` finding; not relabeled as CRITIC_CLEAN |
| GPT-OSS | One request; **REVIEWER_APPROVED / APPROVE** |
| Fresh final verifier | **PASS**, 12/12 tests after reviewer approval; candidate/review bindings verified |
| Trusted checkpoint | Accepted by the controller, validated and matched to the preserved fixture |
| Sequential lifecycle | Qwen, Nemotron and GPT-OSS unloaded in order; native model inventory empty after each unload |
| HotPin resource policy | Top4; **32 GiB hard maximum** read back on the owned reviewer process |
| Final cleanup | Recorded owned processes exited; native model inventory empty, owned Job gone and session ports free |
| Manager reporting | COMPLETED / ALL_CRITERIA_PROVEN, while nested **AC1 NOT_PROVEN / completion null** remains recorded |

The retained MINOR finding states that addition was incorrect for the numeric
product and multiplication aligns with the task objective. The structured
finding, raw wording and reviewer verdict remain preserved in private evidence;
this summary neither changes nor discards them.

**Scope:** one bounded fixture qualification, not general autonomous coding,
arbitrary unattended production readiness or a human semantic approval. The
Manager reporting discrepancy limits summary claims; it does not replace the
independently validated verifier/review/checkpoint evidence.

**Clone readiness: PARTIAL.** Public source and guarded offline checks are
available. The private operator/watchdog helpers, runtime deployment, frozen
qualification fixture and evidence package are not supplied. A clean clone
does not reproduce the full chain. See the
[documentation index](README.md#dependencies-and-missing-integration-assets).

## Historical qualification scope

The following inherited cases retain their original recorded outcomes. They are
separate from Phase 8B Stage 2; no historical verdict has been upgraded.

## What the recorded cases support

| Case | Worker route | Historical report | Limits |
|---|---|---|---|
| TQ-01 | Cline | Ledger fixture, 18 frozen tests, reviewer approval, final verifier and checkpoint | Prompt-only tool restrictions were insufficient confinement |
| TQ-02 | Cline | Required file deleted; verifier failed; no checkpoint | Negative evidence; a zero worker exit was not success |
| TQ-02R | External SafeQwen | 32 frozen tests, CRITIC_CLEAN, REVIEWER_APPROVED, final verifier and checkpoint | One bounded external fixture; migrated source now included; historical deployment not reproduced |

TQ-02R reportedly made one worker attempt with three completion requests:
read, exact edit, submit. Only the implementation file was writable; README and
tests were frozen. The closed worker registry contained read_files, edit_file,
submit. Downstream stage counts were one critic, one reviewer, one final verifier
and one checkpoint. Two unchanged reuse checks reused existing evidence.

**Cloning this repository does not reproduce TQ-02R.** Phase 8A now includes a
[migration of the five components](../SAFEQWEN.md), with separately recorded
source identities and offline tests. The original host deployment, qualified
model/runtime setup, frozen fixture and retained evidence are not shipped. The snapshot is a public preview, not a
production-certified distribution.

## Reporting discrepancy

The historical TQ-02R top-level stop reason is ALL_CRITERIA_PROVEN. Nested AC1–AC3
retain NOT_PROVEN and completion is null. Phase 8B Stage 2 separately retains
ALL_CRITERIA_PROVEN with AC1 NOT_PROVEN / completion null. Legacy counters do
not represent the WorkUnit stages. The source has distinct WorkUnit acceptance and legacy reporting paths.
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
