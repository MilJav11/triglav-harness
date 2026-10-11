# Frozen calculator demo

Copy these three files into a new disposable directory; do not run the coding
controller against this example directory or the TRIGLAV checkout itself.
`add` is implemented; `multiply` deliberately raises NotImplementedError.

From the TRIGLAV root (the destination must not already exist):

```powershell
$fixture = Join-Path (Split-Path (Get-Location).Path -Parent) 'disposable-calculator'
New-Item -ItemType Directory -Path $fixture -ErrorAction Stop | Out-Null
Copy-Item examples/calculator/calculator.py, examples/calculator/test_calculator.py, examples/calculator/.gitignore -Destination $fixture
git -C $fixture init -q
git -C $fixture add -- calculator.py test_calculator.py .gitignore
git -C $fixture -c core.hooksPath=NUL -c commit.gpgSign=false -c user.name='TRIGLAV Demo' -c user.email='demo@localhost' commit -qm 'Frozen calculator baseline'
Push-Location $fixture
python -B -m unittest -v test_calculator.py
Pop-Location
```

Expected baseline: exit 1, **4 passing addition tests and 6 multiplication
errors**. That intentional failure is not a broken setup. Git must be clean
before the controller starts. Record the test identity:

```powershell
git -C $fixture status --short
Get-FileHash (Join-Path $fixture 'test_calculator.py') -Algorithm SHA256
```

The only writable file is `calculator.py`; freeze `test_calculator.py`. After
execution, require 10/10 tests, unchanged test hash and only calculator.py in
`git diff --name-only`. Positive, negative, mixed-sign, zero, fractional and
large-integer cases are covered. See [the local demo](../../docs/WINDOWS_SETUP.md#local-qwen-demo).
