@echo off
REM run.bat — ThreadVault local launcher (Windows Command Prompt)
REM Usage:  run.bat
REM --------------------------------------------------

echo.
echo ==========================================
echo      ThreadVault — Local Setup
echo ==========================================
echo.

REM ── 1. Check Python ──────────────────────────────────────────────────────────
python --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: Python 3 is required but was not found.
    echo Download it from https://www.python.org/downloads/
    pause
    exit /b 1
)

FOR /F "tokens=*" %%i IN ('python --version') DO echo Found: %%i

REM ── 2. Create virtual environment ────────────────────────────────────────────
IF NOT EXIST "venv\" (
    echo Creating virtual environment...
    python -m venv venv
    echo Virtual environment created.
) ELSE (
    echo Virtual environment already exists.
)

REM ── 3. Activate venv ─────────────────────────────────────────────────────────
CALL venv\Scripts\activate.bat
echo Virtual environment activated.

REM ── 4. Install Python dependencies ───────────────────────────────────────────
echo Installing Python dependencies...
pip install -r requirements.txt --quiet --disable-pip-version-check
echo Dependencies installed.

REM ── 5. Install Playwright Chromium ───────────────────────────────────────────
echo Installing Playwright Chromium...
playwright install chromium
echo Playwright Chromium ready.

REM ── 6. Create output directory ────────────────────────────────────────────────
IF NOT EXIST "output\" mkdir output
echo Output directory ready.

REM ── 7. Check for .env credentials ────────────────────────────────────────────
IF NOT EXIST ".env" (
    echo.
    echo WARNING: No .env file found.
    echo Copy .env.example to .env and fill in your Reddit API credentials.
    echo Get them free at: https://www.reddit.com/prefs/apps
    echo.
    IF EXIST ".env.example" (
        COPY ".env.example" ".env" >nul
        echo .env.example was copied to .env -- please edit it now.
    )
)

REM ── 8. Launch the server ──────────────────────────────────────────────────────
echo.
echo Starting ThreadVault...
echo Open your browser at:  http://localhost:8000
echo Press Ctrl+C to stop.
echo.

cd backend
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
