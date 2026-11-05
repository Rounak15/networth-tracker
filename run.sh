#!/usr/bin/env bash
# Net worth tracker launcher for Linux and macOS
set -e
cd "$(dirname "$0")"

PY=$(command -v python3 || command -v python || true)
if [ -z "$PY" ]; then
    echo "Python was not found. Install Python 3.10 or newer (e.g. sudo apt install python3 python3-venv)."
    exit 1
fi

if [ ! -f venv/bin/activate ]; then
    echo "Creating virtual environment..."
    if ! "$PY" -m venv venv; then
        rm -rf venv
        echo "Could not create the virtual environment."
        echo "On Debian/Ubuntu install it with: sudo apt install python3-venv"
        exit 1
    fi
fi

# shellcheck disable=SC1091
source venv/bin/activate

echo "Checking dependencies..."
python -m pip install -q -r requirements.txt || {
    echo "Dependency install failed. Check your internet connection and try again."
    exit 1
}

echo "Starting Net worth tracker (your browser opens once it is ready)... press Ctrl+C or use Log out to stop it"

exec python app.py
