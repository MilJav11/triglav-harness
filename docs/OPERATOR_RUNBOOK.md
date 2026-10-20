# TRIGLAV — Operator Runbook

This guide describes how to operate the TRIGLAV Local AI Long-Horizon Coding Harness from Windows PowerShell using the verified operator wrapper script:

`scripts/Invoke-Triglav.ps1` (or `C:\AI\tools\triglav\Invoke-Triglav.ps1`)

---

## 1. System Requirements & Hardware Gates

- **Supported Platform:** Windows 10/11 Pro 64-bit (x64)
- **Minimum RAM for Single-Model (Qwen):** $\ge 16\text{ GiB}$ free physical RAM
- **Minimum RAM for Three-Model (Qwen + Nemotron + GPT-OSS):** $\ge 44\text{ GiB}$ free physical RAM (strict HotPin reviewer admission gate)
- **GPU:** NVIDIA GPU with $\ge 6\text{ GB}$ VRAM (e.g. RTX A3000)
- **Runtimes & Models:** Local GGUF models in `C:\AI\models\`, `llama.cpp` in `C:\AI\runtimes\llama.cpp\`, HotPin runtime in `C:\AI\runtimes\hotpin\`, and `llama-swap.exe` gateway.

---

## 2. Key Scope & Architectural Constraints

1. **Clean Baseline Repository:**
   The target repository must be a clean Git working copy (`git status --porcelain` must be empty). Dirty repositories are rejected immediately to guarantee deterministic rollbacks.
2. **Existing-File Limitation (Current Release):**
   The SafeQwen executor operates exclusively via anchor replacements (`edit_file`) on files with verified cryptographic digests (`before_sha256`). All files specified in `-AllowPaths` **must already exist** in the repository at baseline. Dynamic file creation is not yet supported.
3. **Deterministic Verification:**
   Every task requires an approved verification command (`-Verify`, e.g. `python -m unittest test_calculator.py`). No changes are accepted without passing deterministic tests.

---

## 3. Operator Workflow

### Step 1: Preflight Dry-Run (Zero Models, Zero Risk)
Always validate arguments, file boundaries, and budgets first without starting gateways or loading models into RAM:

```powershell
scripts\Invoke-Triglav.ps1 `
    -Repo "C:\projects\my-app" `
    -Task "Implement multiply(a, b) in calculator.py without changing add() or frozen tests" `
    -Verify "python -m unittest test_calculator.py" `
    -AllowPaths "calculator.py" `
    -ForbidPaths "test_calculator.py", ".gitignore" `
    -ThreeModel `
    -DryRun
```
- **Exit Code 0:** Configuration and policy are valid.
- **Non-zero Exit:** Parameter or scope error (e.g. dirty repository, missing file, or invalid verification command).

### Step 2: Live Three-Model Execution
Execute the full local AI pipeline:
1. **Qwen3-Coder-30B** explores code, applies diff, and verifies candidate tests.
2. **Nemotron-3.5-30B** evaluates diff as independent critic (`CRITIC_CLEAN`).
3. **GPT-OSS-120B** loads via HotPin ($\le 32\text{ GiB}$ working set) and performs senior review (`APPROVE`).
4. **Final deterministic verification** runs freshly inside an isolated Windows Job Object.
5. **Trusted checkpoint** is sealed in the WAL.

```powershell
scripts\Invoke-Triglav.ps1 `
    -Repo "C:\projects\my-app" `
    -Task "Implement multiply(a, b) in calculator.py without changing add() or frozen tests" `
    -Verify "python -m unittest test_calculator.py" `
    -AllowPaths "calculator.py" `
    -ForbidPaths "test_calculator.py", ".gitignore" `
    -ThreeModel
```

*(For single-model execution without critic and senior reviewer gates, omit `-ThreeModel`.)*

### Step 3: Inspect Diff & Verify
Upon completion, the target repository contains uncommitted modifications strictly confined to `-AllowPaths`:

```powershell
# Review git diff
git -C "C:\projects\my-app" diff

# Manually execute tests
python -m unittest test_calculator.py
```

### Step 4: Accept or Reject Changes
The operator retains exclusive authority over repository commits:

- **To Accept and commit changes:**
  ```powershell
  git -C "C:\projects\my-app" add "calculator.py"
  git -C "C:\projects\my-app" commit -m "TRIGLAV: Implement multiply(a, b)"
  ```
- **To Reject and discard changes:**
  ```powershell
  git -C "C:\projects\my-app" restore "calculator.py"
  ```

### Step 5: Safe Termination
If a task needs to be cancelled during active execution, pressing `Ctrl+C` triggers the PowerShell `finally` handler, which gracefully unloads active models, terminates `llama-swap.exe` and child server processes, and deletes the temporary session PID file.
