@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo Python was not found. Install Python 3.10 or newer from python.org and tick "Add to PATH".
    pause
    exit /b 1
)

if not exist venv\Scripts\activate.bat (
    echo Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo Could not create the virtual environment.
        pause
        exit /b 1
    )
)

call venv\Scripts\activate.bat

echo Checking dependencies...
python -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo Dependency install failed. Check your internet connection and try again.
    pause
    exit /b 1
)

echo Starting Net worth tracker (your browser opens once it is ready)... close this window to stop it
python app.py
if errorlevel 1 pause
