# TRIGLAV safety and limitations

PUBLIC PREVIEW — Phase 4 AS-IS baseline, 9 October 2026.

Claim IDs resolve in the [public evidence summary](EVIDENCE_SUMMARY.md#claim-map).

**SafeQwen is NOT a complete OS sandbox.** It provides a preventive controller tool boundary for the configured worker operations. Controller-owned Python verification may execute repository test code and the edited source. Host isolation and concurrent filesystem mutation remain open limitations. The entire system is not established as secure against arbitrary untrusted repositories. [S02, L05](EVIDENCE_SUMMARY.md#claim-map)

This historical policy description remains relevant background. See
[current SafeQwen permissions, ownership and recovery limits](../SAFEQWEN.md#tool-permissions-and-residual-risks)
for the Phase 8A migration, which replaces global hooks but has no live qualification.

## Exact SafeQwen tool policy

| Operation | Accepted arguments and effect |
|---|---|
| `read_files` | 1–8 exact readable paths; returns UTF-8 text and current SHA-256 |
| `edit_file` | Exact writable path, lowercase 64-character `before_sha256`, nonempty literal `old_text`, bounded `new_text`; one-occurrence replacement in an existing file |
| `submit` | Bounded summary; completion signal with `trusted=false` |

The registry is exactly those three names. No model-facing shell, process launcher, network tool, delete/move/rename, file creation, glob write, plugin loader, MCP or delegation tool is registered. The adapter uses controller-owned Gateway transport; absence of a worker network tool does not mean the entire controller performs no network communication. [S02, S04](EVIDENCE_SUMMARY.md#claim-map)

For TQ-02R, readable/required files are `README.md`, `inventory.py`, `test_inventory.py`; writable is `inventory.py`; frozen files are README and tests. The policy derives from the launcher configuration's exact baseline/frozen partition, rather than model-selected paths. The qualification contract allows one worker attempt. [Q04, S02](EVIDENCE_SUMMARY.md#claim-map)

## Structural enforcement

The model returns one JSON operation. The wire request supplies a strict JSON schema; the controller separately validates fields and types. Duplicate keys, non-finite JSON constants, unknown tools, extra/missing fields, native tool calls, refusals, incomplete finish reasons and malformed envelopes cannot directly dispatch a tool. Invalid operations consume the bounded budget; there is no unrestricted executor fallback. Prompt instructions about treating file contents as untrusted are guidance, not the enforcement boundary. [S02, S04](EVIDENCE_SUMMARY.md#claim-map)

Path checks permit a limited portable character set and exact repository-relative names. They reject absolute/drive/UNC/alternate-stream syntax, backslashes, traversal, empty/dot components, reserved device names, protected metadata names, case aliases, symlinks/reparse components, non-regular files and files with multiple hard links. Targets must already exist and resolve inside the policy root. Frozen write targets are rejected. Required files and frozen content hashes are checked around dispatch and writes. [S02](EVIDENCE_SUMMARY.md#claim-map)

Edits require the current hash and exactly one old-text occurrence. The controller persists intent before writing, flushes a sibling temporary file, rechecks identity/content, atomically replaces the target, checks readback and persists applied evidence. If audit persistence fails, the attempt aborts; an edit already applied before a later evidence failure remains untrusted. Temporary-file deletion belongs to the controller's implementation, not a model deletion capability. [S02](EVIDENCE_SUMMARY.md#claim-map)

## Implemented bounds

| Limit | SafeQwen qualification value |
|---|---|
| Model operations/turns | At most 32, including malformed requests |
| Unit/worker budget | 600 seconds, clipped by controller deadlines |
| Existing file size | 65,536 bytes |
| Operation text | 70,000 characters |
| Old/new text fields | 32,768 characters each |
| Read paths per operation | 8 |
| Path length | 240 characters |
| Submit summary | 1,000 characters |
| Conversation history | 500,000 characters checked before requests |
| Completion request | 4,096 tokens; temperature 0; seed 42; non-streaming |

These bounds are source/config values, not performance measurements. History checks occur before each request; the threshold is not an OS memory ceiling. Seed and temperature do not establish universal deterministic model output. [S02–S04](EVIDENCE_SUMMARY.md#claim-map)

## Identity and acceptance

Five external sources, tool schema/inventory, model routing, scope, policy and limits are fingerprinted and checked against the effective executor. Existing environment and checkpoint bindings are extended in memory for the external worker. Source or policy changes require reviewed new pins and invalidate reuse. The context is single-controller scoped and must surround creation, execution and reload. Model weights in the launcher are pinned by size/mtime, while selected source/runtime files use content hashes; do not describe all runtime assets as independently content-hashed. [S03](EVIDENCE_SUMMARY.md#claim-map)

Controller-owned scope and verifier checks precede reviews. Critic findings are advisory. Senior approval must remain fresh and complete; final verification and candidate stability precede checkpoint acceptance. Hash-linked evidence establishes internal consistency, not a human signature or semantic correctness. Checkpoint cleanup metadata alone is not a process exit proof. [A04–A08, L01–L03](EVIDENCE_SUMMARY.md#claim-map)

## Open limitations

- Tests can execute malicious imports, subprocesses or network effects. `shell=False` and an approved Python command do not sandbox Python code. Windows verifier Job Objects help supervise process lifetime; they do not isolate filesystem or network access.
- Exclusive controller ownership is assumed. Path/hash rechecks reduce stale edits but do not eliminate adversarial TOCTOU swaps or another process mutating files concurrently. The launcher lock does not constrain unrelated processes.
- Atomic replacement does not promise complete ACL/alternate-stream preservation or directory-fsync durability on every filesystem. A crash can leave a generated temporary file or an unmatched audit intent. Storage failure can prevent evidence retention.
- The historical external in-memory identity hooks were not a generic plugin API. Phase 8A replaces them with direct binding; concurrency, filesystem races and intermediate recovery restrictions remain described in the current integration guide.
- Review packets are bounded. Current candidate diff evidence is capped at 6,000 characters and 32 changed files; incomplete code evidence cannot establish new trust. Reviewers can still miss defects in complete packets.
- The successful evidence covers small supervised fixtures. Multi-unit live planning, unattended repair campaigns, concurrent tasks, large-repository reliability, cross-host restoration and every crash/reboot window are unverified.
- Delegated model ownership can become ambiguous. Do not infer process identity from PID alone or terminate unrelated processes. Dedicated gateway ownership matters because unloading affects models on that instance.
- The reserved deep/WSL path is not qualified for automatic execution; explicit tested child termination would be needed.
- UTF-8 existing-file constraints exclude normal creation, binary editing and broad refactors. Manual schema/validator consistency remains a maintenance responsibility.
- Manager criteria/counters disagree with the WorkUnit completion label, and cleanup fields have the qualifications described in [ARCHITECTURE.md](ARCHITECTURE.md) and [QUALIFICATION.md](QUALIFICATION.md).

[L01–L06, S02–S04](EVIDENCE_SUMMARY.md#claim-map)

## Historical lesson

Cline's qualified yolo tools included `run_commands`; the TQ-02 model used it to delete the source file despite a no-commands instruction. A later feasibility report found no qualified fail-closed policy integration for that pinned executable. That historical conclusion applies to the assessed Cline route. SafeQwen's subsequent closed dispatch changes the worker tool boundary; it does not retroactively qualify Cline or supply host isolation. [Q02, S01](EVIDENCE_SUMMARY.md#claim-map)

## Thermal operation

The documented workstation is an HP ZBook Fury 15 G8 with Intel Core i7-11850H, 64 GB RAM and NVIDIA RTX A3000 6 GB. GPT-OSS-120B-Q4_K_M uses HotPin; the reviewer is large relative to available RAM/VRAM and can impose high CPU/RAM load. Throttling and sustained-load thermal risk deserve operator supervision. [H01](EVIDENCE_SUMMARY.md#claim-map)

No TQ-02R temperature trace was found in the supplied evidence. No measured temperatures, throttling events, power draw or thermal benchmark are claimed. Recommended practice is to monitor CPU/GPU temperatures, clocks, throttling flags and memory pressure with HWiNFO, keep airflow unobstructed and use a cooling pad when useful. These are operating recommendations, not measured qualification results.

For sustained throttling, worsening memory pressure or instability, stop the foreground launcher using its interrupt path, allow owned cleanup to finish, and verify ownership/exit evidence before another run. Do not use broad process-name termination. If ownership or exit remains ambiguous, stop further work and inspect it. No numeric “safe temperature” threshold has been validated here.

Future work should evaluate a smaller reviewer against frozen semantic/boundary cases, evidence discipline, cleanup, RAM and thermal behavior. A smaller model is not yet qualified as an equivalent replacement. [H01, R01](EVIDENCE_SUMMARY.md#claim-map)
