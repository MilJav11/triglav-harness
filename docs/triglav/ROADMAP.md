# TRIGLAV roadmap

Public Run 003 completed one bounded public three-model workflow with a trusted
checkpoint; [scope and limits](QUALIFICATION.md#public-run-003) remain explicit.
The current priority is public GitHub readiness: portable setup, reproducible
model-free checks and a bounded local demo. Optional model optimization is deferred.
The remaining items are proposals, not authorization for new models or features.

| Priority | Work | Evidence required |
|---|---|---|
| 1 | Maintain independent brand provenance and license boundaries | Owner approval recorded; retain the MIT/software and separate brand scope |
| 2 | Clear public setup and validated dependency versions | Fresh committed checkout, reviewed configuration and separate host validation |
| 3 | Publicly reproducible SafeQwen deployment and broader qualification | Public Run 003 passed one bounded WorkUnit; new hosts still need licensed assets and independent validation |
| 4 | Consistent Manager criteria/reporting | Separate implementation review preserving the historical discrepancy |
| 5 | Direct cleanup accountability | Failure-path tests and independent process-exit evidence |
| 6 | Stronger semantic oracles | Frozen boundary/adversarial cases preserving the false-trust counterexample |
| 7 | Host isolation and concurrency | Adversarial tests; process ownership alone is insufficient |
| 8 | Smaller reviewer evaluation | Fixed quality/resource/lifecycle comparisons; no interchangeable qualification assumed |
| 9 | Larger, multi-unit and long-horizon workflows | Bounded progression only after separate reproducible evidence |

Reuse established components where a concrete requirement justifies them; review
provenance, licenses, dependencies and policy boundaries before importing code.
No Aider framework adoption or external adapter merge is implied.

Keep raw Stores, private evidence, model outputs, credentials and operational logs
private unless separately reviewed. Human semantic and publication authority
remain distinct from model review and controller checkpoint acceptance.

[Claim map](EVIDENCE_SUMMARY.md#claim-map) · [Qualification](QUALIFICATION.md) ·
[Safety](SAFETY_AND_LIMITATIONS.md) · [Licensing](../../LICENSING.md).
