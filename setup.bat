@echo off
REM Go-Manga Monitor Setup Script for Windows

echo Setting up Go-Manga Monitor...
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH
    echo Please install Python from https://python.org
    pause
    exit /b 1
)

echo Python found: 
python --version

echo.
echo Creating virtual environment...
python -m venv venv

echo.
echo Activating virtual environment...
call venv\Scripts\activate.bat

echo.
echo Upgrading pip...
python -m pip install --upgrade pip

echo.
echo Installing dependencies...
pip install -r requirements.txt

echo.
echo Setup complete!
echo.
echo Next steps:
echo 1. Copy .env.example to .env and fill in your Telegram credentials
echo 2. Run the monitor: python go_manga_monitor.py
echo 3. To schedule: use Task Scheduler or the provided cronjob setup
echo.
pause