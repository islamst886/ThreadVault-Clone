#!/usr/bin/env bash
# run.sh — ThreadVault local launcher (Bash / Git Bash / WSL / macOS / Linux)
# Usage:  bash run.sh
# --------------------------------------------------

set -e  # Exit immediately on any error

PYTHON="python3"
# On Windows Git Bash, python3 might not exist — fall back to python
if ! command -v python3 &>/dev/null; then
    PYTHON="python"
fi

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║          ThreadVault — Local Setup       ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 1. Check Python ───────────────────────────────────────────────────────────
if ! command -v "$PYTHON" &>/dev/null; then
    echo "❌  ERROR: Python 3 is required but was not found."
    echo "    Download it from https://www.python.org/downloads/"
    exit 1
fi

PYTHON_VERSION=$("$PYTHON" --version 2>&1)
echo "✅  Found: $PYTHON_VERSION"

# ── 2. Create virtual environment (skip if already exists) ───────────────────
if [ ! -d "venv" ]; then
    echo "📦  Creating virtual environment..."
    "$PYTHON" -m venv venv
    echo "✅  Virtual environment created."
else
    echo "✅  Virtual environment already exists."
fi

# ── 3. Activate venv ─────────────────────────────────────────────────────────
# Works on Bash (Linux/macOS/WSL) and Git Bash on Windows
if [ -f "venv/Scripts/activate" ]; then
    source venv/Scripts/activate     # Git Bash / Windows
else
    source venv/bin/activate         # Linux / macOS / WSL
fi
echo "✅  Virtual environment activated."

# ── 4. Install Python dependencies ───────────────────────────────────────────
echo "📥  Installing Python dependencies..."
pip install -r requirements.txt --quiet --disable-pip-version-check
echo "✅  Dependencies installed."

# ── 5. Install Playwright Chromium browser ───────────────────────────────────
echo "🌐  Installing Playwright Chromium..."
playwright install chromium
echo "✅  Playwright Chromium ready."

# ── 6. Create directories ─────────────────────────────────────────────────────
mkdir -p output
echo "✅  Output directory ready."

# ── 7. Check for .env credentials ────────────────────────────────────────────
if [ ! -f ".env" ]; then
    echo ""
    echo "⚠️   WARNING: No .env file found."
    echo "    Copy .env.example → .env and fill in your Reddit API credentials."
    echo "    Get them free at: https://www.reddit.com/prefs/apps"
    echo ""
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo "    ℹ️  .env.example was copied to .env — please edit it now."
    fi
fi

# ── 8. Launch the server ──────────────────────────────────────────────────────
echo ""
echo "🚀  Starting ThreadVault..."
echo "    Open your browser at:  http://localhost:8000"
echo "    Press Ctrl+C to stop."
echo ""

cd backend
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
