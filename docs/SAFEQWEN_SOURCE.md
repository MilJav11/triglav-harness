# SafeQwen source migration identity

The preserved TQ-02R executor identity was inspected read-only. Its canonical
identity digest was verified, and SHA-256 of each of the five corresponding
original files matched that record **before** any source was copied or adapted.
Only those identified components and reviewed, sanitized development unit tests
were migrated. No private evidence, reports, source archive, frozen qualification
fixture, machine-specific path or run/chat identifier is distributed.

## Original and migrated bytes

| Component | Historical source SHA-256 | Migrated source SHA-256 |
|---|---|---|
| `safe_qwen_worker.py` | `a28a1ea8cf03636b89174b91d5fbe3d39f54e4389ce3876faf3c69ac3791de45` | `7fb0d082c1fe3a93a200b6168d54421daad42ceb77d39d1f3ab2fff9e91bd26f` |
| `tool_protocol.py` | `489b8c238ea335508d3647a7fd9eba1b17acf8f68e346ff19e43b0c3ba318c2a` | `489b8c238ea335508d3647a7fd9eba1b17acf8f68e346ff19e43b0c3ba318c2a` |
| `integration_shim.py` | `f21f76fb186528de9b5b8f8f363d8271c8b5bdc8dd0e31bc4055e8fbc8aca9e7` | `b94a92f2634c08817b02b25457c2d817388753d313c5c37b03b419ffe9be46df` |
| `safe_executor_binding.py` | `b5e6eb404398dcccf18ead310c072f4ac157d85b9e510a32700f3bb2aa29ad10` | `78ec5600456ba97d4084c867162ceb98031688630f81be92d2fbfe9b05c98585` |
| `terminal_run.py` | `d59cf935823c9eb85166488560d8d8eb176a00e3772e043be2d29c6bee3a9f28` | `73673bfab9ffd101fd0ad5e01fe049890e7df3d8865c0dee8940932a0d3b3bbf` |

[Machine-readable hashes](safeqwen-source-hashes.json) cover the exact migrated
source bytes, with LF line endings fixed by .gitattributes. They identify source,
not a live runtime or qualification result. The controller additionally records
actual loaded-source/configuration/policy/runtime identities for each new run;
those operator-local records are not published.

## Changes and assumptions

| Component | Migration changes |
|---|---|
| `tool_protocol.py` | Bytes and JSON protocol/schema unchanged; LF line endings preserved/enforced |
| `safe_qwen_worker.py` | Preserved tool/file/action policy; network/device repository roots rejected before filesystem access; forbidden scope comparison handles Windows case/directory aliases |
| `integration_shim.py` | Preserved supervised transport/response checks/audit/cleanup; configurable loopback endpoint/model alias; fixed WorkUnit bounds and required-file preflight checked before model lifecycle; diagnostic frame paths reduced to basenames |
| `safe_executor_binding.py` | Replaced private imports and scoped global patching with direct conditional environment/executor bindings; pinned portable source/config/scope identity and explicit gateway parent/command/resource validation |
| `terminal_run.py` | Replaced host-specific launcher with the existing autonomous CLI, operator configuration, one fixed WorkUnit, explicit worker injection, conservative pinned resume, and independent cleanup/idle refusal |

The inherited controller changes are confined to `harness.py` CLI selection,
`manager.py` SafeQwen dispatch/resume requirement, `durable.py` explicit executor
binding/read-only fingerprint selection, `environment.py` conditional identity
capture, and `supervision.py` configured gateway ownership dispatch. Default
custom, opt-in OpenCode and historical Cline paths retain their code paths.
No scheduler, deterministic verifier, critic/reviewer decision protocol or
checkpoint implementation is replaced.

Original files imported only standard-library and local project modules. Review
found no third-party implementation/license notice or credential that needed
importing. Host-specific source roots, hardcoded gateway/model routing, private
launch receipts/evidence paths, fixed qualification checkout assertions and old
process startup/shutdown scripts were excluded or replaced. The existing owner's
personal-project/AI-assisted provenance attestation supplies the eligible
original-code basis; see [licensing](../LICENSING.md). Runtime binaries and model
weights must be acquired and licensed separately. No similarity/rights guarantee
or software grant over brand artwork is added.

Runtime settings, source layout, ownership resolution and recovery behavior have
changed. **These migrated sources are not asserted equivalent to the historically
qualified source set.** Historical TQ-02R reported 32 frozen tests and
`CHAIN_ACCEPTED_AWAITING_HUMAN_AUDIT`; the top-level `ALL_CRITERIA_PROVEN` versus
nested AC1Ă˘â‚¬â€śAC3 `NOT_PROVEN` discrepancy remains unresolved. The private originals
and qualification evidence remain untouched. See [integration and Phase 8B
requirements](SAFEQWEN.md) and [qualification scope](triglav/QUALIFICATION.md).
