$ErrorActionPreference = "Stop"

$python = ".\.venv\Scripts\python.exe"
$lock = ".\requirements.lock"

if (-not (Test-Path $python)) {
  throw "Python interpreter not found at $python"
}

if (-not (Test-Path $lock)) {
  throw "Lock file not found at $lock"
}

& $python -m pip install -r $lock --force-reinstall
& $python -m pip install -e . --force-reinstall
& $python -m pip check

Write-Host "Rollback to locked dependencies completed."
