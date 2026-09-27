<#
.SYNOPSIS
    Builds "dist\RC505 Export Tool.exe".

.DESCRIPTION
    1. Creates .venv (if missing) and installs the pinned packages from requirements-build.txt
    2. Runs the unit tests
    3. Builds a single windowed exe with PyInstaller (rc505_export_tool.spec)
    4. Regenerates THIRD_PARTY_NOTICES.txt and copies it and LICENSE next to the exe
    5. Runs the built exe's --smoke-test to prove the bundled Tk, numpy, PortAudio and LAME work
    6. Prints the exe's size and SHA-256 for publishing alongside the download

.PARAMETER Clean
    Delete and recreate .venv first (use after changing requirements-build.txt).
#>
param([switch]$Clean)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Step($message) { Write-Host "`n==> $message" -ForegroundColor Cyan }

function Invoke-Checked([scriptblock]$Command) {
    # Judge native tools by exit code only: pip and PyInstaller log to stderr, which
    # Windows PowerShell would otherwise turn into a terminating error under "Stop".
    $ErrorActionPreference = "Continue"
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "Command failed with exit code ${LASTEXITCODE}: $Command" }
}

function Find-BasePython {
    # Prefer the py launcher's 3.14; fall back to whatever python is on PATH.
    # Returns @{ Exe = ...; Args = [string[]] } (Args must stay an array: splatting a lone string mangles it).
    $candidates = @(
        @{ Exe = "py"; Args = [string[]]@("-3.14") },
        @{ Exe = "python"; Args = [string[]]@() }
    )
    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate.Exe -ErrorAction SilentlyContinue)) { continue }
        $launcherArgs = $candidate.Args
        $ErrorActionPreference = "Continue"
        & $candidate.Exe @launcherArgs -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
        if ($LASTEXITCODE -eq 0) { return $candidate }
    }
    throw "Python 3.10+ not found. Install it (e.g. winget install Python.Python.3.14) and re-run."
}

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if ($Clean -and (Test-Path .venv)) {
    Step "Removing old .venv"
    Remove-Item -Recurse -Force .venv
}
if (-not (Test-Path $venvPython)) {
    $base = Find-BasePython
    $launcherArgs = $base.Args
    Step "Creating .venv with $($base.Exe) $launcherArgs"
    Invoke-Checked { & $base.Exe @launcherArgs -m venv .venv }
}

Step "Installing pinned build requirements"
Invoke-Checked { & $venvPython -m pip install --quiet --disable-pip-version-check -r requirements-build.txt }

Step "Running unit tests"
Invoke-Checked { & $venvPython -m unittest -q test_rc505 }

Step "Building exe with PyInstaller"
Invoke-Checked { & $venvPython -m PyInstaller --noconfirm --clean --log-level WARN rc505_export_tool.spec }

$exePath = Join-Path $PSScriptRoot "dist\RC505 Export Tool.exe"
if (-not (Test-Path $exePath)) { throw "Build finished but $exePath is missing" }

Step "Writing third-party notices"
Invoke-Checked { & $venvPython make_notices.py THIRD_PARTY_NOTICES.txt }
Copy-Item THIRD_PARTY_NOTICES.txt, LICENSE -Destination dist

Step "Smoke-testing the built exe"
$report = Join-Path $env:TEMP "rc505-smoke-test.txt"
Remove-Item $report -ErrorAction SilentlyContinue
$process = Start-Process -FilePath $exePath -ArgumentList "--smoke-test", "`"$report`"" -Wait -PassThru
if (Test-Path $report) { Get-Content $report | ForEach-Object { Write-Host "    $_" } }
if ($process.ExitCode -ne 0) { throw "Smoke test failed (exit code $($process.ExitCode))" }

$exe = Get-Item $exePath
$hash = (Get-FileHash $exePath -Algorithm SHA256).Hash
Step "Done"
Write-Host ("    {0}  ({1:N1} MB)" -f $exe.FullName, ($exe.Length / 1MB))
Write-Host "    SHA-256: $hash"
