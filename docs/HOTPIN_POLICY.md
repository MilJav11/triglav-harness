# SafeQwen HotPin reviewer resource policy

Phase 2B.2 introduced the closed contract and blocked live dispatch. Its approved
baseline is preserved in local commit `1b61c025318b979a1b4aa6f509f035a36fc8a573`.
Phase 2B.3 implements native enforcement using the existing Gateway, Supervisor
and Windows helpers. **Offline validation is not live qualification.** Public Run 003 subsequently exercised these gates on one operator-provisioned
host; see [the sanitized summary](triglav/QUALIFICATION.md#public-run-003).
Each new deployment still needs provisioned assets and its own live validation.

## Operator contract

Add this block to the operator JSON configuration for an enabled HotPin reviewer:

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

Replace the digest placeholder with the trusted expert-asset identity. A filename
does not prove Top4 content or qualification. Actual bytes must match the supplied
digest; the controller does not establish the provenance of that digest.

All six fields are required and unknown fields are refused. Integer values must
be exactly 1, 44 and 32; booleans and floats are refused. The only policy type is
`hotpin`. SHA-256 is 64 lowercase hexadecimal characters. Relative paths resolve
against the operator configuration directory. Missing assets, directories,
network/device paths, drive-relative paths and mismatched content are refused.
The policy requires an enabled review runtime, distinct model aliases for its
roles, and no legacy profile selection. The three-model example includes
this block with a deliberately invalid digest placeholder; supply the approved
expected identity locally. No asset or host approval is bundled.

## Identity and production routing

The normalized policy is covered by the existing executor/configuration digest.
The environment additionally records the expert SHA-256 identity and the policy
digest under `runtime`. Expert bytes are checked at configuration, capture,
restore and native review. Changes/deletion refuse continuation; runtime drift
also blocks review, including cached reuse. Model weights are not strongly hashed
by this policy contract.

SafeQwen provenance comes from the pinned Store and runtime/identity fields as well
as the executor key. Gateway and worker configurations must match the pinned
configuration. Changing an executor key cannot select the legacy path. The
explicit HotPin policy determines enforcement even if display metadata says
`llama.cpp`; changing pinned metadata is rejected. A declared HotPin reviewer
without the policy is refused. This is not automatic discovery of falsely labelled
native binaries that were initially configured as generic reviewers.

## Native dispatch gates

Before cold startup, native x64 Windows support, the bound expert identity and
known available RAM of at least 44 GiB are required. The existing bounded RAM
recovery wait remains in use. Unknown/nonfinite RAM refuses startup.

After readiness, the Supervisor-observed launch child must be the one selected by
the Gateway: matching run, boot, role, alias, executable and creation identity.
Exactly one active owned model record and one native model process are allowed.
The child must retain its recorded gateway parent identity and lineage. The
running endpoint must contain exactly the ready reviewer alias.

The controller reads inherited `LLAMA_HOT_EXPERTS` and requires the same resolved
local asset path and trusted bytes. Environment reads reject duplicate settings,
incomplete reads and process identity changes. It then applies a 32 GiB hard
working-set maximum, preserving HotPin's existing minimum, and checks the actual
readback: hard-maximum enabled, disable flag absent, positive maximum no greater
than the requested ceiling. Failure prevents reviewer inference.

Both Windows helpers accept the expected executable/creation identity. Each
compares it on the retained handle used for its own environment inspection or
quota operation, before and after that operation; neither trusts a separate PID
probe for that handle. Ownership and expert identity are rechecked around native
enforcement. A durable event records child/process, policy and expert digests and
cap readback before the review gate returns.

Cached-role reuse repeats the native gates. TTL expiry follows the existing
verified unload/preflight/launch/observation path and binds a new child. Startup
RAM is not demanded again while an already-loaded reviewer is reused; the existing
active-RAM floor still applies. `Gateway.chat` passes through role switching
before inference. Failure propagates through the existing reviewer cleanup and
approval/checkpoint gates; uncertain ownership never grants cleanup authority.

## Limits and compatibility

The cap is applied after readiness, before inference. It is a process working-set
ceiling, not a guarantee about memory during loading, committed RAM, VRAM or a
whole process tree. Checks establish identity at controller boundaries; they do
not isolate the host against an external actor mutating processes/files between
checks. Use the existing dedicated gateway assumption.

Code-only SafeQwen, generic reviewers and legacy non-SafeQwen profiles retain
their routes. Legacy Windows helper calls may omit expected identity; the explicit
SafeQwen HotPin route always supplies it. Nonempty SafeQwen `review_profiles`
remain rejected. Source changes invalidate old source pins; retained runs are
never silently repinned. Historical source manifests and evidence are preserved.

## Offline validation

`python -B scripts/ci_offline.py` contains 209 reviewed offline cases: the original
129, 26 policy/identity cases and 54 native regressions. Native cases use tiny
fake assets, fake process records/handles and scripted model responses. They
exercise the actual switching, Windows-helper and controller gates without
starting a real gateway/model or modifying a real process quota.
