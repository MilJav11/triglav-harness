# TRIGLAV release status

As of 20 October 2026. Start with the [README](../README.md); this page is the
dated release locator, not a second qualification ledger.

## Source and merged baseline

The authoritative release checkout is **triglav-release**, based on **main at
c6a90fd** in **MilJav11/triglav-harness**. Treat its root as `<release-root>`;
other installations can use a different directory. A documentation branch may
sit on that baseline. Verify actual branch, HEAD and working changes before work.
Older qualification checkouts and handoffs are historical, not the current source.

| Merged change | Scope |
|---|---|
| PR #8 / f3d354f | Phase 3.1 bounded repair: diagnostic evidence, controller grants, Attempt 2 dispatch and native verification |
| PR #9 / 9c83b40 | PowerShell hardening, operator guide and checkpoint-backed criteria reporting |
| PR #10 / c6a90fd | Adaptive terminal splash, Quiet handling and focused branding/operator tests |

## Current GitHub CI status

**Post-merge CI for c6a90fd: PENDING / UNCONFIRMED.** Earlier passing qualification
belongs to its tested revision. The current workflow defines 390 guarded cases
and **1,296 mandatory qualification IDs**; see [CI](CI.md).

## What is established

- The historical real Qwen executor -> Nemotron advisory critic -> GPT-OSS
  reviewer pipeline completed bounded one- and two-unit tasks. Live 002 remains
  the independently confirmed two-unit terminal milestone. Details belong in the
  [qualification ledger](triglav/QUALIFICATION.md), not a new run report here.
- Merged Phase 3.1 repair has earlier offline qualification. Real-model
  error-driven repair, live interruption/resume, TTL reload and campaigns beyond
  two SafeQwen units remain unqualified. The ordinary operator uses one fixed
  worker attempt; it does not select the explicit repair controller APIs.
- Maintainer visual inspection in **Windows Terminal: PASS** for portrait,
  color and alignment. This does not qualify other terminals; see
  [terminal branding](TERMINAL_BRANDING.md).
- TRIGLAV remains an experimental public preview, not production autonomy or
  an OS sandbox. SafeQwen can replace text in existing files only.

## Trust and operation

`UNIT_VERIFIED` and `READY / MILESTONE_READY` are intermediate progress, not
trusted checkpoints. A checkpoint requires the applicable controller verification,
review and stability gates. **Terminal acceptance is separately opt-in** and
requires its WAL-bound acceptance seal; `COMPLETED` or a checkpoint alone is not
that proof. The historical B2 checkpoint is not a terminal seal.

The WAL is authoritative; state.json is a cache and final-report.json a projection.
PR #9 adds evidence-backed criteria projection; old reports remain unchanged and
cached projections may retain their old values. See [architecture](../ARCHITECTURE.md)
and [validation history](../VALIDATION.md).

Use the tracked **scripts/Invoke-Triglav.ps1** as the canonical operator.
The standalone tools/triglav copy differs and is not synchronized by this update.
[Operator instructions](OPERATOR_RUNBOOK.md) explain host-specific prerequisites,
DryRun session writes, live startup, exit codes and cleanup limits.
Use [portable setup](WINDOWS_SETUP.md) for a different installation.

For repository work, follow [AGENTS.md](../AGENTS.md), preserve dirty/untracked
work and historical evidence, and keep model execution and publishing separately
authorized. The [documentation index](triglav/README.md) scopes older material.
