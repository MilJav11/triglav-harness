# Public evidence summary

PUBLIC PREVIEW. This is an editorial summary inherited from the private project's
reviewed documentation. Raw evidence, private run identifiers, Store references,
fixture commits, private runtime pins and machine paths are intentionally omitted.
Those records were not accessed or replayed during the clean export audit.
This update additionally summarizes the verified Phase 8B Stage 2 final report;
no raw private record is imported and no model qualification is replayed.

## Evidence interpretation

Current source presence, current offline tests, historical reports and proposed
work have different scope. A recorded result is not a reproduction package.
The controller and tests are present. Phase 8A adds a
[reviewed SafeQwen migration](../SAFEQWEN.md) with [separate source hashes](../SAFEQWEN_SOURCE.md);
its offline validation does not reproduce the historical deployment.
The public HEAD `1a3167830f8d1fdf79dba255346784f57793a3ce` passed 129/129
guarded offline cases. [Phase 8B Stage 2](QUALIFICATION.md#phase-8b-stage-2)
then passed one bounded three-model fixture using private operator tooling and
local runtimes: 8 FAIL / 4 PASS → 12/12 PASS, CRITIC_FINDINGS with one retained
MINOR finding, REVIEWER_APPROVED / APPROVE, fresh final verifier PASS and a
validated checkpoint. Sequential unloads and the 32 GiB HotPin cap were verified.
This adds no clean-clone reproduction or general autonomous coding claim.

| Historical case | Reported outcome |
|---|---|
| TQ-01 | Cline ledger fixture; 18 frozen tests, senior approval, final verification and checkpoint |
| TQ-02 | Required file deletion; deterministic rejection, no trusted checkpoint |
| TQ-02R | External SafeQwen fixture; 32 frozen tests, CRITIC_CLEAN, REVIEWER_APPROVED, final verification and checkpoint |

The reported TQ-02R chain contains one worker attempt, three worker completion
requests, one critic, one reviewer, one final-verifier artifact and one checkpoint.
Two unchanged reload/reuse checks reused acceptance evidence; they are not two
new model or verifier runs. None establishes general autonomous reliability.

## Operational limits

The documented external contract used one fixed WorkUnit, one worker attempt,
a closed read_files/edit_file/submit registry, exact existing file scopes and
frozen test files. It was not an OS sandbox. The historically selected reviewer
was GPT-OSS-120B through HotPin.

Controller configuration and effective deadlines must be distinguished.
The source's general reviewer request ceiling differs from its nested reviewer
setting; a setting name alone is not an enforced limit. Current hardware,
latency and thermal behavior were not measured by the export audit. Stage 2
resource/lifecycle evidence is specific to its privately provisioned deployment.

## Reporting and semantic limits

ALL_CRITERIA_PROVEN appears at the top level while nested AC1–AC3 remain NOT_PROVEN
and completion is null in historical TQ-02R. Stage 2 retains the corresponding
AC1 NOT_PROVEN / completion null discrepancy. The distinct WorkUnit/legacy report paths explain the
representation mismatch; this export does not fix it.
QUAL-004 retained a false-trust outcome despite passing tests and reviews.
Approval and evidence hashes cannot prove all application semantics.

## Claim map

Claim IDs provide stable editorial crosslinks within these documents. They are
summary labels, not links to unpublished evidence or independent attestations.

| ID | Public claim and scope |
|---|---|
| **B01** | Exported controller snapshot is distinct from the qualified external deployment |
| **A01** | Ordinary CLI and externally injected SafeQwen WorkUnit routes differ |
| **A02** | Historical external launcher owned preflight, receipts and lifecycle checks |
| **A03** | Worker submission is untrusted; controller verification owns acceptance evidence |
| **A04** | Nemotron criticism is advisory |
| **A05** | Senior approval remains subject to final verification and stability |
| **A06** | Gateway and llama-swap coordinate sequential roles and resource policies |
| **A07** | Completed reuse requires freshness checks beyond loading a Store |
| **A08** | Durable hashes and ledger integrity are bounded, unsigned evidence |
| **Q01** | TQ-01 reported bounded Cline fixture success |
| **Q02** | TQ-02 reported deletion and verifier rejection |
| **Q03** | External SafeQwen development had historical offline and smoke reports |
| **Q04** | TQ-02R reported a fixed SafeQwen bugfix chain |
| **Q05** | Historical documentation reported scoped artifact integrity checks; none was rerun here |
| **S01** | Cline prompt restrictions did not establish preventive no-shell confinement |
| **S02** | Historical SafeQwen had a closed registry and exact file policy, not an OS sandbox |
| **S03** | Historical external source/policy fingerprints used scoped global hooks; Phase 8A replaces those with direct conditional binding, independently validated offline |
| **S04** | Worker output and diagnostics require independent structural validation |
| **L01** | Recorded false trust limits semantic claims |
| **L02** | Top-level and nested criterion reporting remain inconsistent |
| **L03** | Cleanup metadata alone does not prove native process exit |
| **L04** | Configured timeout names do not guarantee every effective deadline |
| **L05** | Repository tests execute code; process ownership is not full host isolation |
| **L06** | Larger, hostile, concurrent and long-horizon workflows remain unqualified |
| **H01** | Historical hardware/reviewer choices do not establish portable resource requirements |
| **R01** | Roadmap items are proposals requiring their own review and evidence |

[Architecture](ARCHITECTURE.md) · [Qualification](QUALIFICATION.md) ·
[Safety](SAFETY_AND_LIMITATIONS.md) · [Roadmap](ROADMAP.md).
