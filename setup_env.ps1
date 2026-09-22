# Autonomous Workstation MCP Hub - environment setup
# Installs pinned Python dependencies and Playwright browser drivers.

Write-Host "=== [1/2] Installing Python dependencies ===" -ForegroundColor Cyan
python -m pip install --upgrade pip --quiet
python -m pip install -r "$PSScriptRoot\requirements.txt"
if ($LASTEXITCODE -ne 0) { Write-Host "pip install FAILED" -ForegroundColor Red; exit 1 }

Write-Host "=== [2/2] Installing Playwright browser binaries ===" -ForegroundColor Cyan
python -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    Write-Host "Playwright chromium install FAILED (CDP mode may still work without it)" -ForegroundColor Yellow
}

Write-Host "=== Setup complete ===" -ForegroundColor Green
