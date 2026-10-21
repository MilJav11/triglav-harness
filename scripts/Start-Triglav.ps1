<#
.SYNOPSIS
    Interactive Windows PowerShell 5.1 front end for the canonical TRIGLAV operator.
.PARAMETER InstallShortcut
    Explicitly install a desktop shortcut; never overwrite an existing shortcut.
#>
[CmdletBinding()]
param([switch]$InstallShortcut, [switch]$OperatorWorker)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:LauncherPath = $PSCommandPath
$script:ReleaseRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$script:ProfileRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'TRIGLAV\Launcher\profiles'

function Get-Field($Object, [string]$Name, $Default = $null) {
    if ($null -ne $Object -and $Object.PSObject.Properties[$Name]) { return ,$Object.$Name }
    return ,$Default
}

function Read-Choice([string]$Prompt, [string]$Default = '') {
    $value = Read-Host "$Prompt$(if ($Default) { " [$Default]" }) (back = cancel)"
    if ($null -eq $value) { throw [IO.EndOfStreamException]::new('Input closed; leaving the launcher.') }
    if ($value.Trim() -eq 'back') { throw [OperationCanceledException]::new('Cancelled; no further execution.') }
    if (-not $value.Trim()) { return $Default }
    return $value.Trim()
}

function Get-GitValue([string]$Repo, [string[]]$Arguments) {
    $value = & git -C $Repo @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Git check failed: $value" }
    return ($value -join "`n").Trim()
}

function Assert-Project([string]$Repo) {
    if (-not (Test-Path -LiteralPath $Repo -PathType Container)) { throw 'Project directory does not exist.' }
    $path = (Get-Item -LiteralPath $Repo).FullName.TrimEnd('\', '/')
    $top = (Get-GitValue $path @('rev-parse', '--show-toplevel')).Replace('/', '\').TrimEnd('\')
    if ($top -ne $path) { throw 'Select the Git repository root directory.' }
    if ((Get-GitValue $path @('rev-parse', '--is-bare-repository')) -ne 'false') { throw 'A working Git repository is required.' }
    $head = Get-GitValue $path @('rev-parse', '--verify', 'HEAD')
    if (Get-GitValue $path @('status', '--porcelain', '--untracked-files=all')) { throw 'Project has changes or untracked files. Review them before starting a new task.' }
    return [pscustomobject]@{ Repo = $path; Head = $head; Branch = (Get-GitValue $path @('rev-parse', '--abbrev-ref', 'HEAD')) }
}

function Assert-Scope([string]$Repo, $Paths, [string]$Label) {
    if ($Paths -isnot [array] -or $Paths.Count -eq 0) { throw "$Label must contain at least one existing file." }
    $result = @()
    foreach ($path in $Paths) {
        if ($path -isnot [string] -or [string]::IsNullOrWhiteSpace($path) -or $path.Length -gt 240) { throw "Invalid $Label path." }
        $relative = $path.Replace('\', '/')
        if ([IO.Path]::IsPathRooted($path) -or $relative -match '(^|/)(\.|\.\.|\.git)(/|$)|[:*?"<>|\x00-\x1f]' -or $relative -match '//|/$') {
            throw "Use a plain repository-relative file path: $path"
        }
        $current = $Repo
        foreach ($part in $relative.Split('/')) {
            $current = Join-Path $current $part
            $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Linked scope paths are not supported: $path" }
        }
        if (-not (Test-Path -LiteralPath $current -PathType Leaf)) { throw "Existing regular file required: $path" }
        $result += $relative
    }
    return @($result | Select-Object -Unique)
}

function Assert-Configuration($Config, [switch]$Profile) {
    $permitted = @('Version', 'Repo', 'Mode', 'AllowPaths', 'ForbidPaths', 'Verify', 'Task')
    if ($null -eq $Config -or @($Config.PSObject.Properties.Name | Where-Object { $_ -notin $permitted }).Count) { throw 'Unsupported profile fields.' }
    if ((Get-Field $Config 'Version') -isnot [int] -or $Config.Version -ne 1) { throw 'Unsupported profile version.' }
    foreach ($name in @('Repo', 'Mode', 'Verify')) {
        if ((Get-Field $Config $name) -isnot [string] -or [string]::IsNullOrWhiteSpace($Config.$name)) { throw "Missing $name." }
    }
    if ($Config.Mode -notin @('Qwen', 'ThreeModel')) { throw 'Unsupported model mode.' }
    if ($Config.Verify.Length -gt 8000) { throw 'Verification command is too long.' }
    $task = Get-Field $Config 'Task' ''
    if ($task -isnot [string] -or $task.Length -gt 16000 -or (-not $Profile -and [string]::IsNullOrWhiteSpace($task))) { throw 'Enter a bounded task description.' }
    $project = Assert-Project $Config.Repo
    $allowed = @(Assert-Scope $project.Repo (Get-Field $Config 'AllowPaths') 'Editable scope')
    $forbidden = @(Assert-Scope $project.Repo (Get-Field $Config 'ForbidPaths') 'Frozen scope')
    if (@($allowed | Where-Object { $_ -in $forbidden }).Count) { throw 'Editable and forbidden scope overlap.' }
    # A target must not contain launcher preferences or controller evidence.
    foreach ($outside in @($script:ProfileRoot, (Join-Path $script:ReleaseRoot 'runs'))) {
        $outsideFull = [IO.Path]::GetFullPath($outside).TrimEnd('\')
        if ($outsideFull -eq $project.Repo -or $outsideFull.StartsWith($project.Repo + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Choose a target outside launcher preferences and controller evidence.'
        }
    }
    return [pscustomobject]@{ Version = 1; Repo = $project.Repo; Mode = $Config.Mode; Task = $task;
        AllowPaths = $allowed; ForbidPaths = $forbidden; Verify = $Config.Verify }
}

function Get-ProfilePath([string]$Name) {
    if ($Name -notmatch '^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$') { throw 'Use 1-48 letters, digits, underscores or hyphens for the profile name.' }
    return Join-Path $script:ProfileRoot ($Name + '.json')
}

function Read-Profile([string]$Name) {
    $path = Get-ProfilePath $Name
    $item = Get-Item -LiteralPath $path
    if ($item.Length -gt 65536 -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Invalid profile file.' }
    return Assert-Configuration (Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json) -Profile
}

function Save-Profile($Config, [string]$Name, [bool]$IncludeTask) {
    $valid = Assert-Configuration $Config -Profile
    $path = Get-ProfilePath $Name
    if (Test-Path -LiteralPath $path) { throw 'Profile already exists. Choose another name.' }
    $data = [ordered]@{ Version = 1; Repo = $valid.Repo; Mode = $valid.Mode; AllowPaths = @($valid.AllowPaths);
        ForbidPaths = @($valid.ForbidPaths); Verify = $valid.Verify }
    if ($IncludeTask) { $data.Task = $valid.Task }
    New-Item -ItemType Directory -Path $script:ProfileRoot -Force | Out-Null
    $temporary = Join-Path $script:ProfileRoot ([Guid]::NewGuid().ToString('N') + '.tmp')
    [IO.File]::WriteAllText($temporary, ($data | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
    # Same-directory atomic rename with no overwrite, including concurrent writers.
    [IO.File]::Move($temporary, $path)
    Write-Host "Profile saved: $Name. Keep credentials out of task text and commands."
}

function Invoke-OperatorWorker {
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    try {
        $reader = [IO.StreamReader]::new([Console]::OpenStandardInput(), [Text.UTF8Encoding]::new($false))
        $request = $reader.ReadToEnd() | ConvertFrom-Json
        $arguments = @{}
        foreach ($property in $request.Arguments.PSObject.Properties) { $arguments[$property.Name] = $property.Value }
        $global:LASTEXITCODE = 0
        & $request.Operator @arguments
        exit $LASTEXITCODE
    } catch {
        # Invoke-Triglav uses Stop + Write-Error before exit, so the explicit
        # controller diagnostic, not powershell.exe's generic exit 1, carries 2.
        $code = 1
        $message = $_.Exception.Message
        if ($message -match '^\[Execution\] TRIGLAV run failed or did not reach COMPLETED \(controller exit code: ([0-9]+)\)\.$') { $code = [int]$Matches[1] }
        Write-Host ('TRIGLAV_LAUNCHER_ERROR: ' + (@{ message = $message; exit_code = $code } | ConvertTo-Json -Compress))
        exit $code
    }
}

function Invoke-Canonical($Config, [bool]$DryRun) {
    $operator = Join-Path $script:ReleaseRoot 'scripts\Invoke-Triglav.ps1'
    if (-not (Test-Path -LiteralPath $operator -PathType Leaf)) { throw "Canonical operator missing: $operator" }
    $arguments = @{ Repo = $Config.Repo; Task = $Config.Task; Verify = $Config.Verify;
        AllowPaths = @($Config.AllowPaths); ForbidPaths = @($Config.ForbidPaths); TriglavRoot = $script:ReleaseRoot }
    if ($Config.Mode -eq 'ThreeModel') { $arguments.ThreeModel = $true }
    if ($DryRun) { $arguments.DryRun = $true }
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $start.Arguments = '-NoLogo -NoProfile -File "' + $script:LauncherPath + '" -OperatorWorker'
    $start.UseShellExecute = $false
    # A launcher started from pwsh must not pass PowerShell 7 module paths to 5.1.
    $start.EnvironmentVariables.Remove('PSModulePath')
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    # Leave stderr on the inherited terminal for the canonical branding splash.
    $start.StandardOutputEncoding = [Text.UTF8Encoding]::new($false)
    $start.WorkingDirectory = $script:ReleaseRoot
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    $ids = @()
    $errorText = ''
    try {
        [void]$process.Start()
        # A fresh no-BOM writer avoids the default writer appending a UTF-8 BOM
        # when Windows console code pages change between invocations.
        $writer = [IO.StreamWriter]::new($process.StandardInput.BaseStream, [Text.UTF8Encoding]::new($false))
        $writer.Write((@{ Operator = $operator; Arguments = $arguments } | ConvertTo-Json -Depth 6 -Compress))
        $writer.Dispose()
        while ($null -ne ($line = $process.StandardOutput.ReadLine())) {
            Write-Host $line
            if ($line -match '^Autonomous run: ([A-Za-z0-9][A-Za-z0-9_-]{0,79})$') { $ids += $Matches[1] }
            if ($line.StartsWith('TRIGLAV_LAUNCHER_ERROR: ')) { $errorText = $line.Substring(24) }
        }
        $process.WaitForExit()
        return [pscustomobject]@{ ExitCode = $process.ExitCode; RunId = $(if ($ids.Count -eq 1) { $ids[0] } else { $null }); Error = $errorText }
    } finally { $process.Dispose() }
}

function Get-SafeRunDirectory([string]$RunId = '') {
    $root = Get-Item -LiteralPath (Join-Path $script:ReleaseRoot 'runs') -Force
    if (-not $root.PSIsContainer -or ($root.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Unsafe canonical runs directory.' }
    if (-not $RunId) { return $root.FullName }
    if ($RunId -notmatch '^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$') { throw 'Invalid run ID.' }
    $directory = Get-Item -LiteralPath (Join-Path $root.FullName $RunId) -Force
    if (-not $directory.PSIsContainer -or ($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $directory.Parent.FullName -ne $root.FullName) {
        throw 'Unsafe run directory; evidence correlation UNKNOWN.'
    }
    return $directory.FullName
}

function Read-RunEvidence([string]$RunId, $Config = $null) {
    if ($RunId -notmatch '^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$') { throw 'Run ID unavailable; evidence correlation UNKNOWN.' }
    [void](Get-SafeRunDirectory $RunId)
    $helper = Join-Path $script:ReleaseRoot 'scripts\launcher_evidence.py'
    $expected = if ($null -eq $Config) { '{}' } else { $Config | ConvertTo-Json -Depth 5 -Compress }
    $priorEncoding = $OutputEncoding
    try {
        $OutputEncoding = [Text.UTF8Encoding]::new($false)
        $json = $expected | & python -B -X utf8 $helper --root $script:ReleaseRoot --run-id $RunId
        if ($LASTEXITCODE -ne 0) { throw 'Evidence reader failed; trust UNKNOWN.' }
        return ($json -join "`n") | ConvertFrom-Json
    } finally { $OutputEncoding = $priorEncoding }
}

function Show-Result($Result, $Evidence = $null) {
    Write-Host "`nOperator exit: $($Result.ExitCode)" -ForegroundColor Cyan
    $knownExit = $Result.ExitCode -is [int]
    $failedExit = $knownExit -and $Result.ExitCode -notin @(0, 2)
    if ($Result.Error) { Write-Host "Operator diagnostic: $($Result.Error)" }
    if ($Result.RunId) { Write-Host "Invocation run ID: $($Result.RunId)"; Write-Host "Evidence: $(Join-Path $script:ReleaseRoot ('runs\' + $Result.RunId))" }
    if ($null -eq $Evidence -or -not (Get-Field $Evidence 'validated' $false)) {
        if ($failedExit) { Write-Host 'Execution outcome: FAILED / HUMAN_ACTION_REQUIRED' -ForegroundColor Yellow }
        Write-Host 'Persisted evidence / checkpoint / acceptance: UNKNOWN. Preserve the candidate and inspect evidence.' -ForegroundColor Yellow
        if ($Evidence) { Write-Host (Get-Field $Evidence 'error' 'Evidence unavailable') }
        return
    }
    # CLI returns 0 for COMPLETED, 2 for any other returned outcome, or 1 on
    # exception. A checkpoint can survive FAILED / INTERRUPTED execution.
    $expectedExits = @(2)
    if ($Evidence.status -eq 'COMPLETED') { $expectedExits = @(0) }
    elseif ($Evidence.status -in @('FAILED', 'HUMAN_ACTION_REQUIRED', 'INTERRUPTED')) { $expectedExits = @(1, 2) }
    $disagreement = $knownExit -and $Result.ExitCode -notin $expectedExits
    $terminalConflict = $Evidence.terminal_accepted -and $Evidence.status -ne 'COMPLETED'
    $label = 'FAILED / HUMAN_ACTION_REQUIRED'
    if (-not ($failedExit -or $disagreement -or $terminalConflict)) {
        if ($Evidence.status -eq 'READY' -and $Evidence.reason -eq 'MILESTONE_READY') { $label = 'VERIFIED INTERMEDIATE CANDIDATE' }
        elseif ($Evidence.status -eq 'COMPLETED') { $label = 'COMPLETED (recorded controller outcome)' }
    }
    Write-Host "Execution outcome: $label" -ForegroundColor Yellow
    if ($disagreement) { Write-Host "Operator/WAL disagreement: exit $($Result.ExitCode), WAL status $($Evidence.status). Inspect both; current success is not established." -ForegroundColor Yellow }
    if (-not $knownExit) { Write-Host 'Operator/WAL comparison: UNKNOWN (historical operator exit unavailable).' }
    if ($Evidence.checkpoint) { Write-Host "Retained trusted checkpoint: $($Evidence.checkpoint) (persisted evidence; does not establish current execution success)." }
    else { Write-Host 'Trusted checkpoint evidence: NONE' }
    if ($terminalConflict) { Write-Host 'Evidence disagreement: terminal acceptance contradicts WAL status. Terminal acceptance evidence: UNKNOWN.' -ForegroundColor Yellow }
    elseif ($Evidence.terminal_accepted) { Write-Host 'Terminal acceptance evidence: VALIDATED (persisted WAL transaction and seal; separate from current execution outcome).' }
    else { Write-Host 'Terminal acceptance evidence: NOT PRESENT' }
    foreach ($key in @('target_repository', 'run_id', 'status', 'phase', 'reason', 'verification', 'cleanup', 'evidence_directory', 'report_status', 'failure')) {
        Write-Host ('{0}: {1}' -f $key, $Evidence.$key)
    }
    Write-Host "Gateway process cleanup: UNKNOWN (operator diagnostics are not independent process evidence)."
    Write-Host "Changed files (recorded): $($Evidence.changed_paths -join ', ')"
    Write-Host 'Intermediate verification is not a checkpoint. A checkpoint alone is not terminal acceptance.'
}

function Invoke-TaskFlow($Config) {
    $valid = Assert-Configuration $Config
    $before = Assert-Project $valid.Repo
    $operatorHash = (Get-FileHash -LiteralPath (Join-Path $script:ReleaseRoot 'scripts\Invoke-Triglav.ps1') -Algorithm SHA256).Hash
    Show-WizardStep 9 'Configuration summary'
    Write-Host "Project: $($valid.Repo)`nModel: $($valid.Mode)`nTask: $($valid.Task)`nVerify: $($valid.Verify)"
    Write-Host 'EDITABLE FILES (allowed to change):' -ForegroundColor Green
    foreach ($path in $valid.AllowPaths) { Write-Host "  - $path" }
    Write-Host 'FORBIDDEN FILES (must stay unchanged):' -ForegroundColor Yellow
    foreach ($path in $valid.ForbidPaths) { Write-Host "  - $path" }
    Write-Host 'Existing files only. Verification runs trusted target code. No automatic retry or rollback.'
    if ($valid.Mode -eq 'ThreeModel') {
        Write-Host 'Three-model mode needs substantially more RAM, time and loading; checkpoint gates still apply.' -ForegroundColor Yellow
        if ((Read-Choice 'Type THREE to opt in to this three-model configuration') -cne 'THREE') { return }
    }
    Show-WizardStep 10 'Mandatory DryRun - no models'
    if ((Read-Choice 'Press Enter for mandatory DryRun, or type back') -ne '') { return }
    $dry = Invoke-Canonical $valid $true
    if ($dry.ExitCode -ne 0) { Show-Result $dry; Write-Host 'DryRun failed. Live execution blocked.'; return }
    Write-Host 'DryRun passed: no inference, no target verification. Operator session configuration was written.' -ForegroundColor Green
    Write-Host "Safety: $($valid.Mode); editable: $($valid.AllowPaths -join ', '); frozen: $($valid.ForbidPaths -join ', ')"
    # Recheck once before prompting and again after confirmation to close prompt-time drift.
    $after = Assert-Project $valid.Repo
    if ($before.Head -ne $after.Head -or $before.Branch -ne $after.Branch) { throw 'Repository identity changed after DryRun. Start again.' }
    Show-WizardStep 11 'Live execution confirmation - Enter cancels'
    if ((Read-Choice 'Type RUN to authorize ONE live inference run; Enter cancels') -cne 'RUN') { Write-Host 'Live run cancelled.'; return }
    [void](Assert-Configuration $valid)
    $after = Assert-Project $valid.Repo
    if ($before.Head -ne $after.Head -or $before.Branch -ne $after.Branch -or $operatorHash -ne (Get-FileHash -LiteralPath (Join-Path $script:ReleaseRoot 'scripts\Invoke-Triglav.ps1') -Algorithm SHA256).Hash) { throw 'Repository or operator changed after DryRun. Start again.' }
    $result = Invoke-Canonical $valid $false
    $evidence = $null
    if ($result.RunId) {
        try { $evidence = Read-RunEvidence $result.RunId $valid }
        catch { $evidence = [pscustomobject]@{ validated = $false; error = $_.Exception.Message } }
    }
    Show-Result $result $evidence
    Write-Host "Current changed files: $(Get-GitValue $valid.Repo @('diff', '--name-only'))"
}

function Show-WizardStep([int]$Number, [string]$Title) {
    Write-Host "`n--- STEP $Number/11: $Title ---" -ForegroundColor Cyan
}

function Assert-FileEntry([string]$Repo, [string]$Entry, $Selected, $OtherScope, [string]$Label) {
    $path = @(Assert-Scope $Repo @($Entry) $Label)[0]
    if ($path -in $Selected) { throw "Already added: $path. Enter a different file, or finish the list." }
    if ($path -in $OtherScope) { throw "Scope overlap: $path is already editable and cannot also be forbidden. Enter a different forbidden file." }
    return $path
}

function Read-FileList([string]$Label, [string]$Repo, $Default, $OtherScope = @()) {
    Write-Host "$Label - enter one existing repository-relative file per line."
    Write-Host 'Press ENTER on an empty line to finish.' -ForegroundColor Cyan
    $paths = @()
    if (@($Default).Count -and $Default) {
        if ((Read-Choice "Reuse $Label list: $($Default -join ', ')? y/n" 'y') -eq 'y') {
            $allValid = $true
            foreach ($entry in $Default) {
                try { $paths += Assert-FileEntry $Repo $entry $paths $OtherScope $Label }
                catch { $allValid = $false; Write-Host "Not added. $($_.Exception.Message)" -ForegroundColor Yellow }
            }
            if ($allValid -and $paths.Count) { return $paths }
            Write-Host 'Keep the valid entries and correct this list below.'
        }
    }
    if ($OtherScope.Count) { Write-Host "Already EDITABLE (cannot be forbidden): $($OtherScope -join ', ')" }
    while ($true) {
        $entry = Read-Choice $Label
        if (-not $entry) {
            if ($paths.Count) { return $paths }
            Write-Host "Add at least one $Label before finishing." -ForegroundColor Yellow
            continue
        }
        try {
            $path = Assert-FileEntry $Repo $entry $paths $OtherScope $Label
            $paths += $path
            Write-Host "Added $Label`: $path"
        } catch {
            Write-Host "Not added. $($_.Exception.Message)" -ForegroundColor Yellow
            Write-Host 'Try again in this step. Your previous entries and task settings are kept.'
        }
    }
}

function Start-TaskWizard($Profile = $null) {
    Show-WizardStep 1 'Project location'
    $repo = Read-Choice 'Project Git root directory' (Get-Field $Profile 'Repo' '')
    Show-WizardStep 2 'Git repository validation'
    [void](Assert-Project $repo)
    Write-Host 'Clean committed Git repository confirmed.'
    Show-WizardStep 3 'Model mode'
    Write-Host '1. Qwen only (default)    2. Three-model (explicit opt-in required)'
    $modeChoice = Read-Choice 'Model mode: 1 or 2' $(if ((Get-Field $Profile 'Mode') -eq 'ThreeModel') { '2' } else { '1' })
    if ($modeChoice -notin @('1', '2')) { throw 'Choose model mode 1 or 2.' }
    Show-WizardStep 4 'Coding task'
    $task = Read-Choice 'Describe the coding task' (Get-Field $Profile 'Task' '')
    Show-WizardStep 5 'EDITABLE FILES - files TRIGLAV may change'
    $allowed = @(Read-FileList 'EDITABLE FILE' $repo (Get-Field $Profile 'AllowPaths' @()))
    Show-WizardStep 6 'FORBIDDEN FILES - files TRIGLAV must not change'
    $forbidden = @(Read-FileList 'FORBIDDEN FILE' $repo (Get-Field $Profile 'ForbidPaths' @()) $allowed)
    Show-WizardStep 7 'Deterministic verification command'
    $verify = Read-Choice 'Deterministic verification command' (Get-Field $Profile 'Verify' '')
    $config = Assert-Configuration ([pscustomobject]@{ Version = 1; Repo = $repo; Mode = $(if ($modeChoice -eq '2') { 'ThreeModel' } else { 'Qwen' });
        Task = $task; AllowPaths = $allowed; ForbidPaths = $forbidden; Verify = $verify })
    Show-WizardStep 8 'Optional project profile'
    if ((Read-Choice 'Save optional project profile? y/n' 'n') -eq 'y') {
        $name = Read-Choice 'New profile name'
        $include = (Read-Choice 'Also save task description? y/n' 'n') -eq 'y'
        Save-Profile $config $name $include
    }
    Invoke-TaskFlow $config
}

function Select-Profile {
    $profiles = @(Get-ChildItem -LiteralPath $script:ProfileRoot -Filter '*.json' -File -ErrorAction SilentlyContinue | Sort-Object Name | Select-Object -First 30)
    if (-not $profiles.Count) { throw 'No profiles saved yet. Start a new task to save one.' }
    for ($i = 0; $i -lt $profiles.Count; $i++) { Write-Host "$($i + 1). $($profiles[$i].BaseName)" }
    $choice = Read-Choice 'Profile number'
    $number = 0
    if (-not [int]::TryParse($choice, [ref]$number) -or $number -lt 1 -or $number -gt $profiles.Count) { throw 'Invalid selection.' }
    return $profiles[$number - 1].BaseName
}

function Show-PreviousRuns {
    $runs = Join-Path $script:ReleaseRoot 'runs'
    if (-not (Test-Path -LiteralPath $runs)) { Write-Host 'No persisted runs found.'; return }
    $runs = Get-SafeRunDirectory
    $items = @(Get-ChildItem -LiteralPath $runs -Directory -ErrorAction SilentlyContinue | Where-Object {
        try { $safe = Get-SafeRunDirectory $_.Name; Test-Path -LiteralPath (Join-Path $safe 'events.jsonl') } catch { $false }
    } | Sort-Object LastWriteTime -Descending | Select-Object -First 20)
    if (-not $items.Count) { Write-Host 'No persisted runs found.'; return }
    Write-Host 'List statuses are recorded report projections; selection validates the WAL.'
    for ($i = 0; $i -lt $items.Count; $i++) {
        $status = 'UNKNOWN'
        try {
            $safe = Get-SafeRunDirectory $items[$i].Name
            $reportFile = Get-Item -LiteralPath (Join-Path $safe 'final-report.json')
            if ($reportFile.Length -le 4MB -and -not ($reportFile.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                $report = Get-Content -LiteralPath $reportFile.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
                if ((Get-Field $report 'run_id') -eq $items[$i].Name -and (Get-Field $report 'status') -match '^[A-Z_]{1,40}$') { $status = $report.status }
            }
        } catch { }
        Write-Host "$($i + 1). $($items[$i].Name) | recorded: $status"
    }
    $choice = Read-Choice 'Inspect run number (latest 20; read only)'
    $number = 0
    if (-not [int]::TryParse($choice, [ref]$number) -or $number -lt 1 -or $number -gt $items.Count) { throw 'Invalid selection.' }
    [void](Get-SafeRunDirectory $items[$number - 1].Name)
    Show-Result ([pscustomobject]@{ ExitCode = 'UNKNOWN (historical)'; Error = ''; RunId = $items[$number - 1].Name }) (Read-RunEvidence $items[$number - 1].Name)
}

function Get-ShortcutSpec([string]$Desktop) {
    if (-not (Test-Path -LiteralPath $Desktop -PathType Container)) { throw 'Desktop folder unavailable.' }
    $link = Join-Path $Desktop 'TRIGLAV.lnk'
    if (Test-Path -LiteralPath $link) { throw 'TRIGLAV.lnk already exists; nothing overwritten.' }
    return [pscustomobject]@{ Path = $link; Target = (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe');
        Arguments = ('-NoLogo -NoProfile -NoExit -File "' + $script:LauncherPath + '"'); WorkingDirectory = $script:ReleaseRoot }
}

function Install-LauncherShortcut {
    $spec = Get-ShortcutSpec ([Environment]::GetFolderPath('DesktopDirectory'))
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($spec.Path)
    $link.TargetPath = $spec.Target
    $link.Arguments = $spec.Arguments
    $link.WorkingDirectory = $spec.WorkingDirectory
    $link.Description = 'TRIGLAV local coding task launcher'
    $link.IconLocation = "$($spec.Target),0"
    # Reserve exclusively before COM Save, so concurrent installations cannot overwrite.
    $reservation = [IO.File]::Open($spec.Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    $reservation.Dispose()
    $link.Save()
    Write-Host "Installed: $($spec.Path)"
}

function Show-LauncherHelp {
    Write-Host @'
TRIGLAV launcher - existing files, deterministic checks, human oversight.
Use back at any prompt to return to the menu. Enter cancels live confirmation.
Qwen normally ends at exit 2: READY / MILESTONE_READY, an intermediate candidate.
Three-model mode has additional RAM/review gates; terminal acceptance is separate.
DryRun starts no models but writes canonical operator session configuration.
Profiles are local JSON preferences; keep secrets out of all saved fields.
Ctrl+C during inference is NOT qualified interruption/resume. Inspect evidence
and owned processes before doing anything further. Never assume rollback.
Direct fallback: scripts\Invoke-Triglav.ps1; see docs\OPERATOR_RUNBOOK.md.
Shortcut: run this launcher with -InstallShortcut after reviewing the source.
'@
    foreach ($command in @('git', 'python', 'powershell.exe')) {
        $found = Get-Command $command -ErrorAction SilentlyContinue
        Write-Host "$command : $(if ($found) { $found.Source } else { 'MISSING' })"
    }
    Write-Host "Canonical operator present: $(Test-Path -LiteralPath (Join-Path $script:ReleaseRoot 'scripts\Invoke-Triglav.ps1'))"
    Write-Host "Profiles: $script:ProfileRoot"
    Write-Host 'The canonical operator validates installed runtime assets. This menu does not probe model readiness.'
}

function Start-Launcher {
    while ($true) {
        # Flowing text avoids cursor positioning or cached viewport dimensions on resize.
        Write-Host "`nTRIGLAV | Local operator" -ForegroundColor Cyan
        Write-Host "1. Start new coding task`n2. Load saved project profile`n3. Inspect previous run results`n4. Manage saved profiles`n5. Help / diagnostics`n0. Exit"
        try {
            switch (Read-Choice 'Choose' '0') {
                '0' { return }
                '1' { Start-TaskWizard }
                '2' { Start-TaskWizard (Read-Profile (Select-Profile)) }
                '3' { Show-PreviousRuns }
                '4' {
                    $name = Select-Profile
                    Write-Host '1. Inspect profile    2. Delete profile    Enter. Return'
                    switch (Read-Choice 'Action') {
                        '1' { Read-Profile $name | Format-List | Out-Host }
                        '2' { if ((Read-Choice "Type DELETE to delete profile $name") -ceq 'DELETE') { Remove-Item -LiteralPath (Get-ProfilePath $name) } }
                    }
                }
                '5' { Show-LauncherHelp }
                default { Write-Host 'Choose a number from 0 to 5.' }
            }
        } catch [IO.EndOfStreamException] { Write-Host $_.Exception.Message; return }
        catch [OperationCanceledException] { Write-Host $_.Exception.Message }
        catch { Write-Host "Stopped: $($_.Exception.Message)" -ForegroundColor Yellow }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    if ($OperatorWorker) { Invoke-OperatorWorker }
    elseif ($InstallShortcut) { Install-LauncherShortcut }
    else { Start-Launcher }
}
