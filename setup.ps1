# One-shot setup for Windows: create .venv and install dependencies.
# Run from PowerShell:  powershell -ExecutionPolicy Bypass -File .\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# "py -3" is the standard launcher; fall back to whatever "python" resolves to.
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -m venv .venv
} else {
    & python -m venv .venv
}

& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

Write-Host ""
Write-Host "Done. Start the web UI with:"
Write-Host "    .\.venv\Scripts\python.exe app.py"
