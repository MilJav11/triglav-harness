# Terminal identity

`scripts/Invoke-Triglav.ps1` displays the original 40 x 62 portrait once, on the
inherited interactive stderr terminal, after writing the runtime configuration
and before starting the controller. The gateway alone redirects its stdout and
stderr to files. Branding never enters that process or controller evidence.
Direct `harness.py` commands retain their existing output contract.

The portrait occupies 40 columns and 31 rows. Gold headings and aligned runtime
fields sit to its right. Branch and commit describe the **target repository**;
model names come from the generated configuration's aliases and display metadata.
Only roles enabled by the invocation are shown as configured: single-model mode
shows critic/reviewer as `OFF`. `STARTING` describes launch, and `DRY RUN` describes
validation; neither implies model readiness, verification, or acceptance.

Windows Terminal (`WT_SESSION`) and VS Code (`TERM_PROGRAM=vscode`) support the
truecolor half-block presentation with UTF-8. Other terminals can advertise
`COLORTERM=truecolor` or `24bit`. A viewport of at least 78 columns by 32 rows is
required; 110 by 38 gives the longer model names more room. Smaller viewports,
`NO_COLOR` (including an empty value), `TERM=dumb`, unknown color support, or
encodings without the half-block character use at most four compact ASCII lines.
The final column and prompt row are reserved to prevent wrapping/overflow.

Redirected stderr and `-Quiet` suppress the splash entirely. `-Quiet` also passes
the existing `--quiet` option to the controller; normal operator diagnostics and
structured results remain available. This does not change the wrapper's existing
diagnostic colors. Display failures are cosmetic and preserve controller cleanup
and exit handling. No dependencies, images, model loads, or network are required.

## Operator preview

From the release checkout in Windows Terminal or the VS Code integrated terminal:

```powershell
# Preview only: reads config and target Git identity; never calls models.
python -B -X utf8 branding.py --repo . --config config/safeqwen.example.json
python -B -X utf8 branding.py --repo . --config config/safeqwen-three-model.example.json --critic --reviewer
```

Replace `--repo .` with the intended target repository. Add `--dry-run` to preview
the validation status. Resize below either minimum dimension to inspect fallback,
and set `$env:NO_COLOR = '1'` to inspect the plain presentation. Remove that
variable afterward if desired. Preview output is on stderr; redirecting stderr
produces no branding output.

## Focused validation

```powershell
python -B -m unittest discover -s tests -p "test_terminal_branding*.py" -v
python -B scripts/ci_supplemental.py check
```

The tests verify every original pixel, palette color, half-block RGB pair, row
reset, alignment, viewport bounds, capabilities, encoding fallback, role flags,
Git identity, single invocation, suppression and output isolation. PowerShell
tests use disposable committed repositories and redirect the fixed session
directory into their fixture. DryRun uses the real controller with inert runtime
files and a matching expert-file digest; resource validation remains active.
Gateway/process and RAM probes are fakes. Simulated controller success, failure
and splash failure exercise the existing cleanup. No live inference is performed.

The focused tests run as a separate CI step. Their IDs are listed in the existing
manifest's `outside_scope` group because they are outside the fixed Phase 3.1
qualification groups, not because they are skipped. Sources, operator script and
workflow are SHA-256 pinned without expanding or weakening existing gates.

GUI visual verification is still required in Windows Terminal and VS Code:
inspect the three heads, golden detail, alignment, colors and wrapping with the
preview commands above. Automated capability/ANSI tests do not prove GUI rendering.
