# TRIGLAV public validation summary

This is a sanitized summary for the clean public snapshot. Original internal
reports, run directories, chat identifiers and machine-local evidence links are
excluded. No private retained evidence was accessed during this export audit.

## Historical Phase 5 offline checks

The export retains the controller implementation and regression tests from the
reviewed source snapshot. The following suites are selected only after reviewing
their dependencies. They use mock/scripted model replies and newly created
disposable fixtures, not live models or retained qualification fixtures.

| Module | Passed cases |
|---|---:|
| test_executor_recovery | 5 |
| test_executor_protocol | 8 |
| test_executor_payloads | 9 |
| test_planner_protocol | 14 |
| test_command_budgets | 4 |
| test_verified_existing | 2 |
| test_candidate_evidence | 9 |
| test_critic_completion | 12 |

During Phase 5 on 2026-10-09, all 63 selected cases passed: zero failures, errors or skipped
cases. CLI help/import output passed under the same guards. All 63 Python files
parsed, all 27 test modules were retained, and all 87 local documentation links
resolved. No executable Python AST changed from the exported source. External
link availability was not validated. Model calls: 0; denied attempts: 0.
The test runner denies socket connect/bind/DNS, unmocked Gateway lifecycle/requests,
and non-Git/non-Python executables. It does not execute gateway startup, native
model runs, inference or thermal measurements.

At the time of the Phase 5 tests, the staging repository had no Git commit. Those tests
use an in-memory shim only for environment.capture's controller HEAD probe. It
returns the exported source identifier for 48 such probes; actual source fingerprints,
test fixture Git operations, assertions and verification remain exercised.
This tests controller logic, not committed-public-repository identity or live setup.

The other 19 regression modules and live scripts were not executed in Phase 5.
They include native lifecycle, optional executor and local HTTP-server cases.
Their complete dependencies need separate review. No full-suite pass is claimed.

## Publication finishing audit

Phase 5.1 on 2026-10-09 validated the entire 93-file candidate: 18 Markdown
documents, 105 local links, 63 parsed Python files, 27 retained test modules,
two safe SVGs and two metadata-free RGBA PNGs. The new independent artwork
was rendered offline and inspected on light and dark backgrounds. Its creation
record remains an attestation. The owner subsequently approved the exact design,
MIT software scope and the revised brand policy in Phase 6.

All software source, tests, scripts and configuration hashes match the Phase 5
candidate. Unit tests were not repeated for these documentation/asset-only changes;
the 63 recorded passes above retain their original guarded scope and HEAD-shim
limitation. No runtime or qualification claim was upgraded. Model calls: 0.

## Approved public launch checks

The owner approved the independent v1 logo, eligible original software under MIT,
the revised separate brand policy and publication as an Experimental Public
Preview on 2026-10-09. This approval does not upgrade historical qualification.

Phase 6 actually executed 22 reviewed offline cases: 8 executor-protocol and
14 planner-protocol tests, with zero failures, errors or skips. CLI help/import
output passed. The same socket/process/unmocked-Gateway guards were used; no
denied attempt occurred and model calls were 0. Because these checks ran before
the initial public commit, 27 exact controller HEAD probes used the test-only
source-identity shim. No real committed-public identity or live setup pass is
claimed. The other 25 test modules were not executed in this launch gate.

The complete 93-file inventory was checked, all 63 Python sources parsed, all
27 test modules were retained, all 105 local links resolved, and both independent
SVGs and metadata-free PNGs passed integrity/safety checks. Source, tests, scripts
and configuration remain unchanged from the preceding approved snapshot.

## Historical external outcomes

| Case | Historical reported result | Public scope |
|---|---|---|
| TQ-01 | Cline ledger fixture; 18 frozen tests, reviewer/final verifier and checkpoint | Historical bounded fixture, not a current public reproduction |
| TQ-02 | Cline deleted a required file; deterministic verification rejected it | Negative tool-safety evidence, no trusted checkpoint |
| TQ-02R | External SafeQwen bugfix; 32 frozen tests, CRITIC_CLEAN, REVIEWER_APPROVED, final verifier and checkpoint | Phase 8A migrates reviewed sources with offline tests; historical deployment/chain is not reproduced |

These statements are inherited sanitized summaries, not fresh verification of
private evidence. Historical review approval is not semantic proof. The earlier
QUAL-004 false-trust case remains a counterexample to broad safety claims.

The reporting discrepancy remains: top-level ALL_CRITERIA_PROVEN, nested AC1–AC3
NOT_PROVEN, and completion=null. No code repair or qualification upgrade is implied.

## Limits

No current model, gateway, third-party CLI, hardware, cross-host restoration,
hostile-repository safety or long-horizon qualification was performed.
No checkpoint or cryptographic digest is a human approval signature.

[Qualification scope](docs/triglav/QUALIFICATION.md) ·
[Safety limits](docs/triglav/SAFETY_AND_LIMITATIONS.md) · [README](README.md).

## Phase 8A SafeQwen migration

Five historical component hashes matched the preserved executor identity before
migration. Only reviewed source/development tests were adapted; no private raw
run, identifier, report, archive or retained fixture was imported. The
[new integration](docs/SAFEQWEN.md) and [source identities](docs/SAFEQWEN_SOURCE.md)
distinguish migrated code from the historical external deployment.

The guarded Windows runner passed 119 selected offline cases (zero failures,
errors or skips): 10 guards, 22 existing protocols, 39 worker cases, 40 integration
cases and 8 selected existing final-verification/checkpoint regressions. Actual
controller HEAD, disposable Git fixtures, scripted wire responses, real
supervision brokers, deterministic verifiers and controller checkpoint logic are
exercised. No model, unmocked Gateway, socket or unreviewed process command ran;
model calls 0. The full suite/native smoke scripts remain outside reviewed scope.

Integration tests explicitly retain rejection of intermediate dirty recovery
without legacy observed-state proof and reject unproven terminal cleanup. Existing
WorkUnit-versus-legacy criterion reporting remains unchanged. Source/configuration,
runtime, policy and actual executor identity gates fail closed. This is offline
integration validation only, not equivalence, live requalification or public-clone
TQ-02R qualification. Phase 8B requires separate owner-authorized live validation.

## Verified readiness and Phase 8B Stage 2

The later readiness assessment verified public HEAD
`1a3167830f8d1fdf79dba255346784f57793a3ce`, 111 tracked files and **129/129**
guarded offline CI cases passed. The current selection adds ten milestone verifier
timeout regressions to the historical 119-case Phase 8A selection; see
[current CI scope](docs/CI.md). Historical counts above remain historical results.

Phase 8B Stage 2 completed successfully on 10 October 2026: one bounded fixture,
8 FAIL / 4 PASS → 12/12 PASS, Nemotron CRITIC_FINDINGS with one preserved MINOR
finding, GPT-OSS REVIEWER_APPROVED / APPROVE, fresh final verifier PASS and a
validated trusted checkpoint. Sequential unloading and the owned HotPin reviewer's
32 GiB cap were verified. AC1 NOT_PROVEN / completion null remains recorded.
See the [qualification summary](docs/triglav/QUALIFICATION.md#phase-8b-stage-2).

This later live qualification supersedes the earlier pending Phase 8B status;
the earlier export/migration checks did not themselves run models. The qualified
deployment still depends on private operator tooling and locally provisioned
model runtimes. **CLONE_READINESS: PARTIAL.** No full-chain clean-clone
reproduction or general autonomous coding qualification is claimed. This
documentation patch runs no models and changes no source, tests or configuration.
