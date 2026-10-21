"""Launcher contracts on Windows PowerShell 5.1; fake operators never load models."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import durable
import harness
import manager
from scripts import launcher_evidence
from test_harness import CONFIG

ROOT = Path(__file__).resolve().parents[1]
PS = shutil.which("powershell.exe")


@unittest.skipUnless(os.name == "nt" and PS, "Windows PowerShell 5.1 required")
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="triglav-launcher-")
        self.addCleanup(self.temp.cleanup)
        # Hosted Windows TEMP may use an 8.3 alias; PowerShell expands it.
        self.root = Path(self.temp.name).resolve()
        self.release = self.root / "release space ž"
        scripts = self.release / "scripts"
        scripts.mkdir(parents=True)
        shutil.copyfile(ROOT / "scripts/Start-Triglav.ps1", scripts / "Start-Triglav.ps1")
        self.repo = self.root / "project space ž"
        self.repo.mkdir()
        (self.repo / "calc ž.py").write_text("# unchanged\n", encoding="utf-8")
        (self.repo / "test_calc.py").write_text("# frozen\n", encoding="utf-8")
        self.git("init", "-q", "-b", "fixture")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.config = dict(Version=1, Repo=str(self.repo), Task="Inspect ž calculator", Mode="Qwen",
                           AllowPaths=["calc ž.py"], ForbidPaths=["test_calc.py"], Verify="python -m unittest test_calc.py")
        self.control = dict(dry=0, live=0, throw=False, ids=["owned-run"])
        self.operator = scripts / "Invoke-Triglav.ps1"
        self.operator.write_text(r'''
param($Repo,$Task,$Verify,[string[]]$AllowPaths,[string[]]$ForbidPaths,$TriglavRoot,[switch]$ThreeModel,[switch]$DryRun)
$ErrorActionPreference='Stop'
$c = Get-Content -LiteralPath (Join-Path $PSScriptRoot '..\control.json') -Raw -Encoding UTF8 | ConvertFrom-Json
@{ Repo=$Repo; Task=$Task; Verify=$Verify; AllowPaths=$AllowPaths; ForbidPaths=$ForbidPaths; ThreeModel=[bool]$ThreeModel; DryRun=[bool]$DryRun } |
    ConvertTo-Json -Compress | Add-Content -LiteralPath (Join-Path $PSScriptRoot '..\calls.jsonl') -Encoding UTF8
if ($DryRun) { if ($c.dry) { Write-Error 'dry validation rejected' }; $global:LASTEXITCODE=0; return }
foreach ($id in $c.ids) { Write-Output "Autonomous run: $id" }
if ($c.throw) { Write-Error "[Execution] TRIGLAV run failed or did not reach COMPLETED (controller exit code: $($c.live))." }
exit $c.live
''', encoding="utf-8-sig")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), "-c", "core.hooksPath=NUL",
                                        "-c", "commit.gpgsign=false", "-c", "user.name=Launcher Test",
                                        "-c", "user.email=launcher@example.invalid", *args], stderr=subprocess.STDOUT)

    def ps(self, body, *, inputs=()):
        (self.release / "control.json").write_text(json.dumps(self.control), encoding="utf-8")
        (self.release / "config.json").write_text(json.dumps(self.config), encoding="utf-8")
        (self.release / "inputs.json").write_text(json.dumps(list(inputs)), encoding="utf-8")
        driver = self.release / "driver.ps1"
        driver.write_text(r'''
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
. (Join-Path $PSScriptRoot 'scripts\Start-Triglav.ps1')
$script:ProfileRoot=Join-Path $PSScriptRoot 'profiles'
$cfg=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$script:answers=New-Object 'System.Collections.Generic.Queue[string]'
@(Get-Content -LiteralPath (Join-Path $PSScriptRoot 'inputs.json') -Raw -Encoding UTF8 | ConvertFrom-Json) | ForEach-Object { foreach ($answer in $_) { $script:answers.Enqueue($answer) } }
function Read-Host { param($Prompt); if (-not $script:answers.Count) { throw 'Unexpected prompt' }; $script:answers.Dequeue() }
''' + body, encoding="utf-8-sig")
        env = {k: v for k, v in os.environ.items() if k.upper() != 'PSMODULEPATH'}
        try:
            result = subprocess.run([PS, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(driver)],
                                    capture_output=True, encoding="utf-8", errors="replace", timeout=30, env=env)
        except subprocess.TimeoutExpired as exc:
            self.fail(f"PowerShell timeout: {str(exc.stdout)[:4000]} {str(exc.stderr)[:1000]}")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def calls(self):
        path = self.release / "calls.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines()] if path.exists() else []

    def show_result(self, code, status, *, checkpoint="checkpoints/0001.json", terminal=False, reason="failure"):
        evidence = dict(validated=True, status=status, phase="READY", reason=reason,
                        target_repository=str(self.repo), run_id="owned-run", verification="passed",
                        cleanup="passed", checkpoint=checkpoint, terminal_accepted=terminal,
                        evidence_directory="fixture", report_status="recorded", failure="recorded detail", changed_paths=[])
        result = dict(ExitCode=code, Error="", RunId="owned-run")
        return self.ps("$e = @'\n" + json.dumps(evidence) + "\n'@ | ConvertFrom-Json\n"
                       "$r = @'\n" + json.dumps(result) + "\n'@ | ConvertFrom-Json\nShow-Result $r $e")

    def reject(self, change):
        self.ps(change + "\ntry { $null=Assert-Configuration $cfg; throw 'NOT_REJECTED' } catch { if ($_.Exception.Message -eq 'NOT_REJECTED') { throw }; Write-Host 'rejected' }")
        self.assertEqual(self.calls(), [])

    def test_invalid_missing_and_non_git_project(self):
        self.reject("$cfg.Repo=Join-Path $PSScriptRoot 'missing'")
        self.reject("$cfg.Repo=$PSScriptRoot")

    def test_dirty_target_rejected_without_modification(self):
        (self.repo / "calc ž.py").write_text("# dirty\n", encoding="utf-8")
        self.reject("")
        self.assertEqual((self.repo / "calc ž.py").read_text(), "# dirty\n")

    def test_missing_scope_and_empty_verifier_rejected(self):
        for change in ("$cfg.AllowPaths=@()", "$cfg.AllowPaths=@('missing.py')", "$cfg.ForbidPaths=@()", "$cfg.Verify=' '"):
            with self.subTest(change=change):
                self.reject(change)

    def test_overlap_and_path_escape_rejected(self):
        for value in ("test_calc.py", "../test_calc.py", "C:/Windows/win.ini", ".git/config", "./calc.py", "calc.py:stream", "a//b"):
            with self.subTest(value=value):
                self.reject("$cfg.AllowPaths=@('" + value + "')")

    def test_qwen_paths_unicode_and_spaces_and_no_repo_changes(self):
        out = self.ps("$r=Invoke-Canonical (Assert-Configuration $cfg) $true; Write-Host ('EXIT='+$r.ExitCode)")
        self.assertIn("EXIT=0", out)
        call = self.calls()[0]
        self.assertEqual(call["Repo"], str(self.repo))
        self.assertEqual(call["Task"], self.config["Task"])
        self.assertEqual(call["AllowPaths"], self.config["AllowPaths"])
        self.assertFalse(call["ThreeModel"])
        self.assertEqual(self.git("status", "--porcelain"), b"")

    def test_failed_dry_run_blocks_live(self):
        self.control["dry"] = 1
        out = self.ps("Invoke-TaskFlow $cfg", inputs=[""])
        self.assertIn("Live execution blocked", out)
        self.assertEqual([c["DryRun"] for c in self.calls()], [True])

    def test_cancellation_after_dry_run_and_before_dry_run(self):
        out = self.ps("Invoke-TaskFlow $cfg", inputs=["", ""])
        self.assertIn("Live run cancelled", out)
        self.assertEqual(len(self.calls()), 1)
        self.ps("try { Invoke-TaskFlow $cfg } catch [OperationCanceledException] { Write-Host 'cancelled' }", inputs=["back"])
        self.assertEqual(len(self.calls()), 1)

    def test_three_model_requires_explicit_opt_in(self):
        self.config["Mode"] = "ThreeModel"
        self.ps("Invoke-TaskFlow $cfg", inputs=[""])
        self.assertEqual(self.calls(), [])
        self.ps("Invoke-TaskFlow $cfg", inputs=["THREE", "", ""])
        self.assertTrue(self.calls()[0]["ThreeModel"])
        self.assertTrue(self.calls()[0]["DryRun"])

    def test_exact_dry_live_configuration_and_unknown_correlation(self):
        out = self.ps("Invoke-TaskFlow $cfg", inputs=["", "RUN"])
        self.assertEqual(len(self.calls()), 2)
        dry, live = self.calls()
        dry.pop("DryRun")
        self.assertFalse(live.pop("DryRun"))
        self.assertEqual(dry, live)
        self.assertIn("UNKNOWN", out)  # fake run has no evidence reader/store
        self.assertEqual(self.git("status", "--porcelain"), b"")

    def test_repository_changed_at_confirmation_blocks_live(self):
        out = self.ps(r'''
function Read-Host { param($Prompt)
    $value=$script:answers.Dequeue()
    if ($value -eq 'RUN') { Add-Content -LiteralPath (Join-Path $cfg.Repo 'test_calc.py') '# drift' }
    return $value
}
try { Invoke-TaskFlow $cfg; throw 'NOT_REJECTED' } catch { if ($_.Exception.Message -eq 'NOT_REJECTED') { throw }; Write-Host 'drift blocked' }
''', inputs=["", "RUN"])
        self.assertIn("drift blocked", out)
        self.assertEqual(len(self.calls()), 1)

    def test_worker_preserves_zero_two_fatal_and_throwing_operator(self):
        for code, throws in ((0, False), (2, False), (2, True), (7, False), (1, True)):
            with self.subTest(code=code, throws=throws):
                self.control.update(live=code, throw=throws)
                out = self.ps("$r=Invoke-Canonical $cfg $false; Write-Host ('EXIT='+$r.ExitCode); Write-Host ('RUN='+$r.RunId)")
                self.assertIn(f"EXIT={code}", out)
                self.assertIn("RUN=owned-run", out)

    def test_multiple_ids_never_select_latest_run(self):
        self.control["ids"] = ["owned-run", "other-run"]
        out = self.ps("$r=Invoke-Canonical $cfg $false; if ($r.RunId) { throw 'misattributed' }; Write-Host 'UNKNOWN'")
        self.assertIn("UNKNOWN", out)

    def test_missing_operator_blocks_invocation(self):
        self.operator.rename(self.operator.with_suffix(".disabled"))
        self.ps("try { Invoke-Canonical $cfg $true; throw 'NOT_REJECTED' } catch { if ($_.Exception.Message -eq 'NOT_REJECTED') { throw } }")
        self.assertEqual(self.calls(), [])

    def test_profiles_opt_in_task_atomic_no_overwrite_and_corruption(self):
        self.ps(r'''
Save-Profile $cfg 'safe' $false
$saved=Get-Content (Get-ProfilePath 'safe') -Raw | ConvertFrom-Json
if ($saved.PSObject.Properties['Task']) { throw 'task persisted without consent' }
$null=Read-Profile 'safe'
Save-Profile $cfg 'with-task' $true
if ((Read-Profile 'with-task').Task -ne $cfg.Task) { throw 'task not saved' }
try { Save-Profile $cfg 'safe' $true; throw 'NOT_REJECTED' } catch { if ($_.Exception.Message -eq 'NOT_REJECTED') { throw } }
Set-Content -LiteralPath (Get-ProfilePath 'bad') -Value '{bad'
try { $null=Read-Profile 'bad'; throw 'NOT_REJECTED' } catch { if ($_.Exception.Message -eq 'NOT_REJECTED') { throw } }
try { $null=Get-ProfilePath '../escape'; throw 'NOT_REJECTED' } catch { if ($_.Exception.Message -eq 'NOT_REJECTED') { throw } }
''')
        self.assertEqual(self.calls(), [])
        self.assertEqual(list((self.release / "profiles").glob("*.tmp")), [])

    def test_profile_schema_rejects_credentials_and_bad_types(self):
        for change in ("$cfg.Version=2", "$cfg.Mode='automatic'", "$cfg.AllowPaths='calc.py'",
                       "$cfg | Add-Member NoteProperty ApiKey 'secret'", "$cfg.Task=123"):
            with self.subTest(change=change):
                self.reject(change)

    def test_profile_storage_cannot_be_the_target_repository(self):
        self.reject('$script:ProfileRoot=$cfg.Repo')
        self.assertEqual(self.git('status', '--porcelain'), b'')

    def test_shortcut_spec_has_no_side_effects_and_refuses_overwrite(self):
        self.ps(r'''
$spec=Get-ShortcutSpec $PSScriptRoot
if ($spec.Arguments -notlike '*-NoExit -File "*Start-Triglav.ps1"' -or $spec.Arguments -match 'Bypass|ExecutionPolicy') { throw 'unsafe shortcut' }
if (Test-Path -LiteralPath $spec.Path) { throw 'shortcut installed by preview' }
[IO.File]::WriteAllText($spec.Path, 'keep')
try { $null=Get-ShortcutSpec $PSScriptRoot; throw 'NOT_REJECTED' } catch { if ($_.Exception.Message -eq 'NOT_REJECTED') { throw } }
if ([IO.File]::ReadAllText($spec.Path) -ne 'keep') { throw 'shortcut overwritten' }
''')

    def test_menu_invalid_input_help_and_exit(self):
        out = self.ps("Start-Launcher", inputs=["invalid", "5", "0"])
        self.assertIn("Choose a number from 0 to 5", out)
        self.assertIn("Help / diagnostics", out)
        self.assertEqual(self.calls(), [])

    def test_closed_input_leaves_menu_without_retry(self):
        out = self.ps("function Read-Host { return $null }; Start-Launcher")
        self.assertIn("Input closed", out)
        self.assertEqual(out.count('TRIGLAV | Local operator'), 1)
        self.assertEqual(self.calls(), [])

    def test_full_wizard_and_loaded_profile_stop_after_fresh_dry_run(self):
        out = self.ps("Start-TaskWizard", inputs=[str(self.repo), '', 'Inspect calculator', 'calc ž.py', '',
                                                 'test_calc.py', '', self.config['Verify'], 'n', '', ''])
        self.assertIn('Live run cancelled', out)
        self.assertEqual([c['DryRun'] for c in self.calls()], [True])
        self.ps("Save-Profile $cfg 'reuse' $false; Start-TaskWizard (Read-Profile 'reuse')",
                inputs=['', '', 'New task after profile load', 'y', 'y', '', 'n', '', ''])
        self.assertEqual([c['DryRun'] for c in self.calls()], [True, True])

    def test_wizard_scope_errors_correct_in_place_without_losing_settings(self):
        task = 'Keep this task while correcting scope'
        out = self.ps(r'''
$script:prompts=New-Object 'System.Collections.Generic.List[string]'
function Read-Host { param($Prompt)
    $script:prompts.Add($Prompt)
    if (-not $script:answers.Count) { throw 'Unexpected prompt' }
    $script:answers.Dequeue()
}
Start-TaskWizard
foreach ($prefix in @('Project Git root', 'Model mode:', 'Describe the coding task')) {
    if (@($script:prompts | Where-Object { $_.StartsWith($prefix) }).Count -ne 1) { throw 'Wizard restarted and lost settings' }
}
if ($script:answers.Count) { throw 'Wizard did not consume the correction flow' }
''', inputs=[str(self.repo), '1', task, '', 'missing.py', '../outside.py', 'calc ž.py', 'calc ž.py', '',
             'calc ž.py', 'test_calc.py', 'test_calc.py', '', self.config['Verify'], 'n', '', ''])
        for number in range(1, 12):
            self.assertEqual(out.count(f'--- STEP {number}/11:'), 1)
        self.assertIn('Press ENTER on an empty line to finish.', out)
        self.assertIn('Added EDITABLE FILE: calc ž.py', out)
        self.assertIn('Added FORBIDDEN FILE: test_calc.py', out)
        self.assertIn('Already added:', out)
        self.assertIn('Scope overlap: calc ž.py is already editable and cannot also be forbidden', out)
        self.assertIn('Your previous entries and task settings are kept.', out)
        self.assertIn('EDITABLE FILES (allowed to change):', out)
        self.assertIn('FORBIDDEN FILES (must stay unchanged):', out)
        self.assertIn('DryRun passed', out)
        self.assertIn('Live run cancelled', out)
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]['DryRun'])
        self.assertFalse(calls[0]['ThreeModel'])
        self.assertEqual(calls[0]['Task'], task)
        self.assertEqual(calls[0]['Repo'], str(self.repo))
        self.assertEqual(calls[0]['AllowPaths'], ['calc ž.py'])
        self.assertEqual(calls[0]['ForbidPaths'], ['test_calc.py'])
        self.assertEqual(calls[0]['Verify'], self.config['Verify'])
        self.assertEqual(self.git('status', '--porcelain'), b'')

    def test_reused_file_list_validates_each_entry_and_keeps_valid_files(self):
        out = self.ps(r'''
$paths=@(Read-FileList 'FORBIDDEN FILE' $cfg.Repo @('calc ž.py', 'test_calc.py', 'test_calc.py', 'missing.py') @('calc ž.py'))
if ($paths.Count -ne 1 -or $paths[0] -ne 'test_calc.py') { throw 'Invalid reused scope accepted' }
''', inputs=['y', ''])
        self.assertIn('Scope overlap:', out)
        self.assertIn('Already added:', out)
        self.assertIn('Keep the valid entries', out)
        self.assertEqual(self.calls(), [])

    def test_back_after_scope_error_cancels_without_preflight_or_inference(self):
        out = self.ps("try { Start-TaskWizard } catch [OperationCanceledException] { Write-Host 'scope entry cancelled' }",
                      inputs=[str(self.repo), '1', 'Preserve this task', 'calc ž.py', '', 'calc ž.py', 'back'])
        self.assertIn('Scope overlap:', out)
        self.assertIn('scope entry cancelled', out)
        self.assertEqual(self.calls(), [])

    def test_result_intermediate_is_not_generic_failure_or_acceptance(self):
        out = self.ps(r'''
$e=[pscustomobject]@{ validated=$true; status='READY'; phase='READY'; reason='MILESTONE_READY';
 target_repository=$cfg.Repo; run_id='owned-run'; verification='UNIT_VERIFIED'; cleanup='passed'; checkpoint=$null;
 terminal_accepted=$false; evidence_directory='fixture'; report_status='recorded'; failure=''; changed_paths=@() }
Show-Result ([pscustomobject]@{ ExitCode=2; Error=''; RunId='owned-run' }) $e
''')
        self.assertIn("VERIFIED INTERMEDIATE CANDIDATE", out)
        self.assertNotIn("FAILED / HUMAN_ACTION_REQUIRED", out)
        self.assertNotIn("Task completed successfully", out)

    def test_failed_execution_keeps_retained_checkpoint_separate(self):
        for code, status in ((1, "FAILED"), (2, "FAILED"), (2, "HUMAN_ACTION_REQUIRED"), ("UNKNOWN (historical)", "INTERRUPTED")):
            with self.subTest(code=code, status=status):
                out = self.show_result(code, status)
                self.assertIn("Execution outcome: FAILED / HUMAN_ACTION_REQUIRED", out)
                self.assertIn("Retained trusted checkpoint: checkpoints/0001.json", out)
                self.assertIn("does not establish current execution success", out)
                self.assertIn("failure: recorded detail", out)
                self.assertNotIn("Execution outcome: TRUSTED CHECKPOINT", out)
                self.assertNotIn("Execution outcome: COMPLETED", out)
                self.assertNotIn("Operator/WAL disagreement", out)

    def test_exit_wal_disagreement_is_explicit_and_never_success(self):
        for code, status in ((0, "FAILED"), (2, "COMPLETED"), (1, "COMPLETED"), (0, "READY"), (1, "READY")):
            with self.subTest(code=code, status=status):
                out = self.show_result(code, status, reason="MILESTONE_READY")
                self.assertIn(f"Operator/WAL disagreement: exit {code}, WAL status {status}", out)
                self.assertIn("Execution outcome: FAILED / HUMAN_ACTION_REQUIRED", out)
                self.assertIn(f"Operator exit: {code}", out)
                self.assertIn(f"status: {status}", out)
                self.assertNotIn("Execution outcome: COMPLETED", out)
                self.assertNotIn("Execution outcome: VERIFIED INTERMEDIATE CANDIDATE", out)

    def test_terminal_artifact_never_overrides_failed_execution_or_status(self):
        out = self.show_result(1, "COMPLETED", terminal=True)
        self.assertIn("Execution outcome: FAILED / HUMAN_ACTION_REQUIRED", out)
        self.assertIn("Operator/WAL disagreement", out)
        self.assertIn("Terminal acceptance evidence: VALIDATED", out)
        self.assertNotIn("Execution outcome: TERMINAL ACCEPTANCE", out)
        out = self.show_result(1, "FAILED", terminal=True)
        self.assertIn("Execution outcome: FAILED / HUMAN_ACTION_REQUIRED", out)
        self.assertIn("Evidence disagreement: terminal acceptance contradicts WAL status", out)
        self.assertIn("Terminal acceptance evidence: UNKNOWN", out)
        self.assertNotIn("Terminal acceptance evidence: VALIDATED", out)

    def test_completed_outcome_and_terminal_artifacts_are_separate(self):
        for terminal in (False, True):
            with self.subTest(terminal=terminal):
                out = self.show_result(0, "COMPLETED", terminal=terminal)
                self.assertIn("Execution outcome: COMPLETED (recorded controller outcome)", out)
                self.assertIn("Retained trusted checkpoint: checkpoints/0001.json", out)
                self.assertIn("Terminal acceptance evidence: " + ("VALIDATED" if terminal else "NOT PRESENT"), out)
                self.assertNotIn("Operator/WAL disagreement", out)

    def test_intermediate_candidate_with_retained_checkpoint_stays_intermediate(self):
        out = self.show_result(2, "READY", reason="MILESTONE_READY")
        self.assertIn("Execution outcome: VERIFIED INTERMEDIATE CANDIDATE", out)
        self.assertIn("Retained trusted checkpoint: checkpoints/0001.json", out)
        self.assertNotIn("FAILED / HUMAN_ACTION_REQUIRED", out)
        self.assertNotIn("Operator/WAL disagreement", out)

    def test_historical_selector_rejects_run_directory_junctions(self):
        out = self.ps(r'''
$runs=Join-Path $PSScriptRoot 'runs'
$outside=Join-Path $PSScriptRoot 'outside'
New-Item -ItemType Directory -Path $outside,(Join-Path $runs 'safe-run') -Force | Out-Null
Set-Content -LiteralPath (Join-Path $outside 'events.jsonl') -Value 'preserve outside WAL'
Set-Content -LiteralPath (Join-Path $outside 'final-report.json') -Value '{"run_id":"unsafe-run","status":"COMPLETED"}'
Set-Content -LiteralPath (Join-Path $runs 'safe-run\events.jsonl') -Value 'fixture WAL'
New-Item -ItemType Junction -Path (Join-Path $runs 'unsafe-run') -Target $outside | Out-Null
try { $null=Get-SafeRunDirectory 'unsafe-run'; throw 'NOT_REJECTED' } catch { if ($_.Exception.Message -eq 'NOT_REJECTED') { throw } }
try { $null=Read-RunEvidence 'unsafe-run'; throw 'NOT_REJECTED' } catch { if ($_.Exception.Message -eq 'NOT_REJECTED') { throw } }
function Read-RunEvidence { param($RunId)
    if ($RunId -ne 'safe-run') { throw 'unsafe selection' }
    [pscustomobject]@{ validated=$false; error='fixture evidence' }
}
Show-PreviousRuns
if ((Get-Content -LiteralPath (Join-Path $outside 'events.jsonl') -Raw).Trim() -ne 'preserve outside WAL') { throw 'outside evidence changed' }
''', inputs=['1'])
        self.assertIn('1. safe-run | recorded: UNKNOWN', out)
        self.assertNotIn('unsafe-run', out)
        self.assertEqual(self.calls(), [])

    def test_historical_selector_rejects_linked_runs_root(self):
        self.ps(r'''
$outside=Join-Path $PSScriptRoot 'outside'
New-Item -ItemType Directory -Path $outside -Force | Out-Null
New-Item -ItemType Junction -Path (Join-Path $PSScriptRoot 'runs') -Target $outside | Out-Null
try { Show-PreviousRuns; throw 'NOT_REJECTED' } catch { if ($_.Exception.Message -eq 'NOT_REJECTED') { throw } }
''')
        self.assertEqual(self.calls(), [])


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="triglav-launcher-evidence-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        from test_manager import git_fixture
        self.repo, _ = git_fixture(self.root)
        config = copy.deepcopy(CONFIG)
        repo = harness.Repository(self.repo, config, harness.EventLog(self.root / "preflight.jsonl"))
        self.store = durable.Store(self.root / "runs", "owned-run")
        manager.create_run(self.store, repo, "Inspect calculator", [["python", "-m", "unittest", "test_add"]],
                           config, criteria=["tests pass"], allow=["calc.py"], forbid=["test_add.py"])

    def inspect(self, expected=None):
        return launcher_evidence.inspect_run(self.root, "owned-run", expected)

    def test_wal_read_does_not_generate_or_rewrite_evidence(self):
        before = {p: p.read_bytes() for p in self.store.directory.rglob("*") if p.is_file()}
        result = self.inspect()
        self.assertTrue(result["validated"])
        self.assertIsNone(result["checkpoint"])
        self.assertFalse(result["terminal_accepted"])
        self.assertEqual(before, {p: p.read_bytes() for p in self.store.directory.rglob("*") if p.is_file()})

    def test_missing_malformed_and_forged_report_cannot_promote_trust(self):
        path = self.store.directory / "final-report.json"
        for content in (None, "{bad", json.dumps(dict(run_id="other", status="COMPLETED", terminal_accepted=True))):
            with self.subTest(content=content):
                if content is not None:
                    path.write_text(content)
                result = self.inspect()
                self.assertFalse(result["terminal_accepted"])
                self.assertIsNone(result["checkpoint"])

    def test_corrupt_wal_checkpoint_and_orphan_seal_fail_closed(self):
        wal = self.store.directory / "events.jsonl"
        original = wal.read_bytes()
        wal.write_bytes(original + b"unfinished")
        with self.assertRaises(ValueError):
            self.inspect()
        records = original.splitlines(keepends=True)
        records[0] = records[0].replace(b'"sequence": 1', b'"sequence": 9', 1).replace(b'"sequence":1', b'"sequence":9', 1)
        self.assertNotEqual(b"".join(records), original)
        wal.write_bytes(b"".join(records))
        with self.assertRaises(durable.DurableError):
            self.inspect()
        wal.write_bytes(original)
        acceptance = self.store.directory / "acceptance"
        acceptance.mkdir(exist_ok=True)
        (acceptance / "term-forged.json").write_text("{}")
        with self.assertRaises(ValueError):
            self.inspect()

    def test_expected_invocation_is_bound_to_repo_task_scope_mode_and_verifier(self):
        expected = dict(Repo=str(self.repo), Task="Inspect calculator", Mode="Qwen", AllowPaths=["calc.py"],
                        ForbidPaths=["test_add.py"], Verify="python -m unittest test_add")
        self.assertTrue(self.inspect(expected)["validated"])
        for field, wrong in (("Repo", str(self.root)), ("Task", "unrelated"), ("Mode", "ThreeModel"),
                             ("AllowPaths", ["test_add.py"]), ("Verify", "python -m unittest other")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.inspect({**expected, field: wrong})

    def test_existing_checkpoint_and_terminal_validators_remain_authority(self):
        from test_durable import TerminalAcceptanceTests
        fixture = TerminalAcceptanceTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture._prepare_verified_state(fixture.store, fixture.repo)
        run_id = fixture.store.directory.name
        result = launcher_evidence.inspect_run(fixture.root, run_id)
        self.assertIsNotNone(result["checkpoint"])
        self.assertFalse(result["terminal_accepted"])
        fixture.store.commit_terminal("acc-launcher", controller=fixture.CTRL, status="COMPLETED")
        result = launcher_evidence.inspect_run(fixture.root, run_id)
        self.assertTrue(result["terminal_accepted"])
        seal = fixture.store.directory / "acceptance/acc-launcher.json"
        seal.write_text("{}")
        with self.assertRaises(durable.DurableError):
            launcher_evidence.inspect_run(fixture.root, run_id)

    @unittest.skipUnless(os.name == 'nt' and PS, 'Windows junction required')
    def test_evidence_reader_rejects_linked_runs_root_and_run_directory(self):
        linked_root = self.root / 'linked-root'
        linked_root.mkdir()
        env = {k: v for k, v in os.environ.items() if k.upper() != 'PSMODULEPATH'}
        env['TRIGLAV_LAUNCHER_LINK_TEST_ROOT'] = str(self.root)
        commands = r'''$root=$env:TRIGLAV_LAUNCHER_LINK_TEST_ROOT
$runs=Join-Path $root 'runs'
New-Item -ItemType Junction -Path (Join-Path $root 'linked-root\runs') -Target $runs | Out-Null
New-Item -ItemType Junction -Path (Join-Path $runs 'linked-run') -Target (Join-Path $runs 'owned-run') | Out-Null
'''
        subprocess.run([PS, '-NoProfile', '-NonInteractive', '-Command', "$ErrorActionPreference='Stop'; " + commands],
                       env=env, check=True, capture_output=True, timeout=15)
        before = {p: p.read_bytes() for p in self.store.directory.rglob('*') if p.is_file()}
        for root, run_id in ((linked_root, 'owned-run'), (self.root, 'linked-run')):
            with self.subTest(root=root), self.assertRaises((ValueError, durable.DurableError)):
                launcher_evidence.inspect_run(root, run_id)
        self.assertEqual(before, {p: p.read_bytes() for p in self.store.directory.rglob('*') if p.is_file()})


if __name__ == "__main__":
    unittest.main()
