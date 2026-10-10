# SafeQwen HotPin policy contract (Phase 2B.2)

This slice implements validation and identity binding only. **Live SafeQwen HotPin
review is blocked until native runtime enforcement receives a separate review.**
No configuration switch enables it. Dry-run is available and reports
`native_enforcement: not_implemented` and `dispatch: blocked`.

## Operator configuration contract

Add a `safe_qwen_reviewer_policy` object to the operator JSON configuration when
its enabled review runtime is HotPin:

```json
{
  "safe_qwen_reviewer_policy": {
    "version": 1,
    "type": "hotpin",
    "hot_experts": "../runtime/hot_top4.txt",
    "hot_experts_sha256": "<trusted lowercase 64-character SHA-256>",
    "min_available_ram_gb": 44,
    "max_working_set_gb": 32
  }
}
```

Replace the hash placeholder with the trusted identity of the intended expert
asset. A filename does not prove Top4 content or qualification. The controller
verifies actual file bytes against the supplied digest; it does not establish
the provenance of that digest.

All six fields are required; unknown fields are refused. Version, RAM minimum and
working-set maximum must be integers exactly 1, 44 and 32, respectively (booleans
and floats are refused). The only supported type is `hotpin`. The digest must be
64 lowercase hexadecimal characters. Missing assets, directories, invalid paths,
network/device paths and mismatched bytes are refused. Drive-relative paths are
unsupported. Relative paths resolve against the operator configuration directory,
not the controller working directory. The policy requires an enabled review
runtime and cannot select legacy review profiles.

## Identity and execution boundary

The normalized policy is covered by the existing configuration/executor digest.
The environment additionally records the expert file's strong SHA-256 identity
and the resource-policy digest under `runtime`. Expert bytes are checked when
configuring, capturing identity and restoring a worker. Changed or deleted expert
files refuse resume before controller execution. No model weights are hashed by
this policy contract.

A complete policy can pass configuration and dry-run validation. Live CLI execution
is refused before creating a run or Gateway. Bound execution, Gateway review
switching (including cached roles), and direct Supervisor review launch also
refuse the incomplete native route. These guards do not apply resource limits or
inspect a live process.

Declared `model_metadata[roles.review].runtime: HotPin` with an enabled review
runtime requires the policy. The explicit policy itself also selects HotPin even
if display metadata differs. Metadata is operator-supplied, not automatic binary
discovery; a falsely labelled native runtime cannot be detected by this slice.
Code-only SafeQwen and generic profile-free reviewers retain their existing paths.
Nonempty `review_profiles` remain rejected. Existing three-model templates declare
HotPin but lack the new policy; they now require the block even for review dry-run,
and remain unable to run live.

Source changes intentionally invalidate older SafeQwen source pins; do not silently
repin retained runs. Historical migration hashes remain preserved, with this
slice's hashes recorded separately in `safeqwen-source-hashes.json`.

## Separate remaining native work

Before removing the block, implement the 44 GiB startup preflight, prove the
Supervisor-owned reviewer process/parent identity, verify its inherited
`LLAMA_HOT_EXPERTS` against the bound local asset, and apply/read back the 32 GiB
hard working-set maximum. Windows helpers must compare the expected process
identity using the same handle used for inspection or quota changes. Cached-role
requests and TTL reloads must repeat the appropriate guards. Preserve current
ownership-safe unload and fail-closed cleanup.

The existing helper applies its cap after readiness, before inference. It does
not establish a startup-wide memory ceiling or a limit on commit, VRAM or the
whole process tree. This slice adds no watchdog, broker or framework and makes
no live qualification claim.

## Offline checks

`python -B scripts/ci_offline.py` runs the original 129 guarded cases plus 26
contract/identity/compatibility regressions. New tests use tiny disposable expert
and runtime files. Real Gateway operations remain prohibited by the CI guard.
