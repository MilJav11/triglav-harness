"""PowerShell operator integration in disposable repos; live processes are fakes."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import branding

ROOT = Path(__file__).resolve().parents[1]
OPERATOR = ROOT / "scripts/Invoke-Triglav.ps1"


class OperatorBrandingTests(unittest.TestCase):
    def setUp(self):
        self.pwsh = shutil.which("pwsh")
        if not self.pwsh or os.name != "nt":
            self.skipTest("Windows PowerShell integration requires Windows and pwsh")
        self.temp = tempfile.TemporaryDirectory(prefix="triglav-branding-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "target repo"
        self.repo.mkdir()
        for name in ("calc.py", "test_calc.py"):
            (self.repo / name).write_text("# branding fixture\n", encoding="utf-8")
        def git(*args):
            return subprocess.check_output(["git", "-C", str(self.repo), "-c", "core.hooksPath=NUL",
                                            "-c", "commit.gpgsign=false", *args], text=True).strip()
        git("init", "-b", "branding-target")
        git("add", ".")
        git("-c", "user.name=Branding Test", "-c", "user.email=branding@example.invalid",
            "commit", "-qm", "fixture")
        self.head = git("rev-parse", "--short=7", "HEAD")

    def run_operator(self, *, three=False, quiet=False, dry=True, fail_splash=False, controller_exit=0):
        # The release fixes its session directory under C:\AI\sessions. Redirect only
        # that destination into this owned fixture; execute all operator logic intact.
        source = OPERATOR.read_text(encoding="utf-8")
        old = '$sessionDir = "C:\\AI\\sessions\\triglav-$timestamp"'
        self.assertEqual(source.count(old), 1)
        source = source.replace(old, "$sessionDir = $env:TRIGLAV_BRANDING_SESSION")
        wrapper = self.root / "operator.ps1"
        wrapper.write_text(source, encoding="utf-8")
        state_path = self.root / "state.json"
        params = dict(Repo=str(self.repo), Task="Inspect calculator", Verify="python -m unittest test_calc.py",
                      AllowPaths=["calc.py"], ForbidPaths=["test_calc.py"], TriglavRoot=str(ROOT),
                      DryRun=dry, ThreeModel=three, Quiet=quiet)
        (self.root / "params.json").write_text(json.dumps(params), encoding="utf-8")
        driver = self.root / "driver.ps1"
        driver.write_text(r'''
$ErrorActionPreference = 'Stop'
$global:brandingState = @{ splash = 0; controller = 0; stopped = 0; unloaded = 0 }
function global:Get-CimInstance { @{ FreePhysicalMemory = 64 * 1024 * 1024 } }
function global:Get-NetTCPConnection { }
function global:Start-Sleep { }
function global:Start-Process { [pscustomobject]@{ Id = 12345; HasExited = $false } |
    Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param($timeout) } -PassThru }
function global:Stop-Process { $global:brandingState.stopped++ }
function global:Invoke-WebRequest {
    if ($args -contains 'Post') { $global:brandingState.unloaded++ }
    [pscustomobject]@{ StatusCode = 200 }
}
function global:python {
    # Native invocation flattens argument arrays; reproduce that at this function seam.
    $nativeArgs = @(foreach ($arg in $args) { foreach ($part in $arg) { $part } })
    if (@($nativeArgs | Where-Object { "$_" -like '*branding.py' }).Count) {
        $global:brandingState.splash++
        if ($env:TRIGLAV_BRANDING_FAIL -eq '1') { throw 'Cosmetic preview failure' }
        & $env:TRIGLAV_BRANDING_PYTHON @nativeArgs
    } else {
        $global:brandingState.controller++
        $global:brandingState.controller_args = $nativeArgs
        if ($nativeArgs -contains '--dry-run') { & $env:TRIGLAV_BRANDING_PYTHON @nativeArgs }
        else { $global:LASTEXITCODE = [int]$env:TRIGLAV_BRANDING_EXIT }
    }
}
$params = Get-Content (Join-Path $PSScriptRoot 'params.json') -Raw | ConvertFrom-Json -AsHashtable
$result = 0
try { & (Join-Path $PSScriptRoot 'operator.ps1') @params }
catch { $result = 1; Write-Host $_ }
$global:brandingState | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $PSScriptRoot 'state.json')
exit $result
''', encoding="utf-8")
        env = dict(os.environ, TRIGLAV_BRANDING_SESSION=str(self.root / "session"),
                   TRIGLAV_BRANDING_PYTHON=sys.executable, TRIGLAV_BRANDING_FAIL=str(int(fail_splash)),
                   TRIGLAV_BRANDING_EXIT=str(controller_exit), PYTHONDONTWRITEBYTECODE="1",
                   PYTHONUTF8="1", NO_COLOR="1")
        result = subprocess.run([self.pwsh, "-NoProfile", "-NonInteractive", "-File", str(driver)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                env=env, timeout=45)
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
        self.assertFalse((self.root / "session/gateway.pid").exists())
        self.assertNotIn("\x1b", result.stdout + result.stderr)
        for path in (self.root / "session").glob("*.json"):
            self.assertNotIn("\x1b", path.read_text())
        return result, state

    def check_dry_run(self, three):
        result, state = self.run_operator(three=three)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"models_contacted": false', result.stdout)
        self.assertEqual((state["splash"], state["controller"], state["stopped"]), (1, 1, 0))
        config = json.loads((self.root / "session/safeqwen.json").read_text())
        mode, names = branding.runtime_info(config, critic=three, reviewer=three)
        self.assertEqual(mode, "THREE-MODEL" if three else "SINGLE-MODEL")
        self.assertEqual(names["critic"] == "OFF", not three)
        self.assertEqual(branding.git_identity(self.repo), ("branding-target", self.head))

    def test_real_single_model_dry_run(self):
        self.check_dry_run(False)

    def test_real_three_model_dry_run(self):
        self.check_dry_run(True)

    def test_quiet_suppresses_preview_and_reaches_controller(self):
        result, state = self.run_operator(quiet=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(state["splash"], 0)
        self.assertIn("--quiet", state["controller_args"])

    def test_cosmetic_failure_does_not_change_cleanup_or_success(self):
        result, state = self.run_operator(dry=False, fail_splash=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((state["splash"], state["controller"], state["stopped"], state["unloaded"]), (1, 1, 1, 1))

    def test_controller_failure_still_cleans_up(self):
        result, state = self.run_operator(dry=False, controller_exit=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((state["stopped"], state["unloaded"]), (1, 1))
        self.assertIn("controller exit code: 2", result.stdout)

    def test_controller_exit_and_cleanup_source_unchanged(self):
        tail = OPERATOR.read_text(encoding="utf-8").split("# DryRun branch", 1)[1]
        self.assertEqual(hashlib.sha256(tail.encode()).hexdigest(), "6c3ef6241f508ebd383a44c0fa6bc10f2d8bd9d241b3ef8a3c6dcd3695318377")


if __name__ == "__main__":
    unittest.main()
