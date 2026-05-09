# run.ps1 — ThreadVault local launcher (Windows PowerShell)
# Usage:  .\run.ps1
#
# If blocked by execution policy, run once as Administrator:
#   Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
# --------------------------------------------------

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "      ThreadVault - Local Setup" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

# -- 1. Check Python ----------------------------------------------------------
try {
    $pyVersion = & python --version 2>&1
    Write-Host "[OK] Found: $pyVersion" -ForegroundColor Green
} catch {
    Write-Host "[ERROR] Python 3 is required but was not found." -ForegroundColor Red
    Write-Host "        Download it from https://www.python.org/downloads/"
    Read-Host "Press Enter to exit"
    exit 1
}

# -- 2. Create virtual environment --------------------------------------------
if (-Not (Test-Path "venv")) {
    Write-Host "[...] Creating virtual environment..." -ForegroundColor Yellow
    python -m venv venv
    Write-Host "[OK] Virtual environment created." -ForegroundColor Green
} else {
    Write-Host "[OK] Virtual environment already exists." -ForegroundColor Green
}

# -- 3. Activate venv ---------------------------------------------------------
Write-Host "[...] Activating virtual environment..." -ForegroundColor Yellow
& "venv\Scripts\Activate.ps1"
Write-Host "[OK] Virtual environment activated." -ForegroundColor Green

# -- 4. Install Python dependencies -------------------------------------------
Write-Host "[...] Installing Python dependencies..." -ForegroundColor Yellow
pip install -r requirements.txt --quiet --disable-pip-version-check
Write-Host "[OK] Dependencies installed." -ForegroundColor Green

# -- 5. Install Playwright Chromium -------------------------------------------
Write-Host "[...] Installing Playwright Chromium..." -ForegroundColor Yellow
playwright install chromium
Write-Host "[OK] Playwright Chromium ready." -ForegroundColor Green

# -- 6. Create output directory -----------------------------------------------
if (-Not (Test-Path "output")) {
    New-Item -ItemType Directory -Path "output" | Out-Null
}
Write-Host "[OK] Output directory ready." -ForegroundColor Green

# -- 7. Check for .env credentials --------------------------------------------
if (-Not (Test-Path ".env")) {
    Write-Host ""
    Write-Host "[WARNING] No .env file found." -ForegroundColor Yellow
    Write-Host "          Copy .env.example to .env and add your Reddit API credentials."
    Write-Host "          (ThreadVault now uses the public JSON API, so .env is optional)"
    Write-Host ""
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
        Write-Host "[INFO] .env.example was copied to .env" -ForegroundColor Cyan
    }
}

# -- 8. Launch the server -----------------------------------------------------
Write-Host ""
Write-Host "[>>>] Starting ThreadVault..." -ForegroundColor Cyan
Write-Host "      Open your browser at:  http://localhost:8000" -ForegroundColor White
Write-Host "      Press Ctrl+C to stop the server." -ForegroundColor White
Write-Host ""

Set-Location backend
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
