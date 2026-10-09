# TRIGLAV architecture

This public snapshot contains the real Python controller, not a demonstration UI.
Its model roles, selected executor and trusted controller checks have distinct authority.

| Source | Responsibility |
|---|---|
| harness.py | CLI, custom JSON executor, repository tools, Gateway transport and ordinary workflow |
| manager.py, recovery.py, planner_protocol.py | Bounded ManagerLoop, recovery observations, planning protocol and gates |
| work_units.py, work_unit_planner.py, work_unit_scheduler.py | WorkUnit contracts, planning, scoped scheduling and verifier dispatch |
| live_work_unit_executor.py | Retained Cline adapter; external CLI output is untrusted |
| opencode_executor.py | Pinned opt-in OpenCode episodes and snapshots |
| critic.py, critic_executor.py | Advisory critic protocol and evidence |
| reviewer_executor.py, candidate_evidence.py | Senior review packets, freshness, final verification and checkpoint bindings |
| durable.py, environment.py | Store, recovery, ledger/cache and environment identity |
| supervision.py, supervision_worker.py, winprocess.py | Native Windows ownership, command brokers and process evidence |

The default CLI uses the custom executor. Its proposals must pass deterministic
verification and the configured review policy. Reviewer approval is followed by
final verification and candidate stability checks before controller acceptance.

```mermaid
flowchart LR
  P["Bounded controller plan"] --> E["Selected executor: untrusted edits"]
  E --> V["Deterministic verification"]
  V --> C["Advisory critic when required"]
  C --> R["Configured senior review"]
  R --> F["Final verification and stability"]
  F --> K["Controller checkpoint for durable routes"]
  K --> H["Human semantic review"]
```

This diagram shows the successful path, not every command or failure branch.
Failed verification, stale bindings, scope failures and ownership ambiguity cannot
be treated as trusted progress. Ordinary run and durable WorkUnit routes have
different state/report structures.

The historical SafeQwen route used an external fixed launcher and worker adapter.
Those components are absent here. The public [historical architecture overview](docs/triglav/ARCHITECTURE.md)
describes that route explicitly; it is not a supported clone-and-run installation.

Nemotron is advisory. GPT-OSS cannot override a failed deterministic verifier.
Tests and hashes provide bounded evidence, not an OS sandbox or semantic proof.
The known top-level/nested criterion discrepancy remains documented.

See [runbook](RUNBOOK.md), [durable state](DURABLE_RUNS.md),
[supervision boundaries](PROCESS_SUPERVISION.md), [OpenCode boundaries](OPENCODE_BOUNDARY.md),
[validation scope](VALIDATION.md), and [safety](docs/triglav/SAFETY_AND_LIMITATIONS.md).
