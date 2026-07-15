#!/usr/bin/env python3
"""
Go-Manga Update Monitor - Cronjob Setup
Creates a scheduled task to run the monitor periodically.
"""

import os
import sys
import subprocess
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.absolute()
PYTHON_EXE = sys.executable
SCRIPT_PATH = PROJECT_DIR / "go_manga_monitor.py"
VENV_PYTHON = PROJECT_DIR / "venv" / "Scripts" / "python.exe"

def create_windows_task():
    """Create a Windows Task Scheduler task."""
    # Use the venv python if available, otherwise system python
    python_exe = str(VENV_PYTHON) if VENV_PYTHON.exists() else PYTHON_EXE
    
    task_name = "GoMangaMonitor"
    # Run every 30 minutes
    schedule = "SC MINUTE MO 30"
    
    # Build command
    cmd = f'"{python_exe}" "{SCRIPT_PATH}"'
    
    # Create the task
    # Using schtasks command
    create_cmd = [
        "schtasks", "/Create",
        "/TN", task_name,
        "/TR", cmd,
        "/SC", "MINUTE",
        "/MO", "30",
        "/F",  # Force overwrite
        "/RL", "HIGHEST",  # Run with highest privileges
    ]
    
    print(f"Creating Windows Task: {task_name}")
    print(f"Command: {cmd}")
    print(f"Schedule: Every 30 minutes")
    
    result = subprocess.run(create_cmd, capture_output=True, text=True, shell=True)
    
    if result.returncode == 0:
        print("✅ Task created successfully!")
        print(f"Run 'schtasks /Run /TN {task_name}' to test")
    else:
        print(f"❌ Failed to create task:")
        print(result.stderr)
        return False
    
    return True

def create_linux_cron():
    """Create a cron job for Linux/macOS (via WSL or similar)."""
    python_exe = str(VENV_PYTHON) if VENV_PYTHON.exists() else PYTHON_EXE
    cmd = f"{python_exe} {SCRIPT_PATH}"
    
    # Every 30 minutes
    cron_line = f"*/30 * * * * {cmd} >> {PROJECT_DIR}/monitor.log 2>&1\n"
    
    print("Add this line to your crontab (run 'crontab -e'):")
    print(cron_line)
    
    # Also save to a file for reference
    cron_file = PROJECT_DIR / "cronjob.txt"
    cron_file.write_text(cron_line)
    print(f"Saved to: {cron_file}")

def main():
    print("=" * 60)
    print("Go-Manga Monitor - Scheduler Setup")
    print("=" * 60)
    print(f"Project: {PROJECT_DIR}")
    print(f"Script:  {SCRIPT_PATH}")
    print(f"Python:  {PYTHON_EXE}")
    print(f"Venv:    {VENV_PYTHON} {'✓' if VENV_PYTHON.exists() else '✗'}")
    print()
    
    if os.name == "nt":
        print("Windows detected - using Task Scheduler")
        create_windows_task()
    else:
        print("Linux/macOS detected - using cron")
        create_linux_cron()
    
    print()
    print("Manual run: python go_manga_monitor.py")
    print("View logs:  type monitor.log (Windows) / tail -f monitor.log (Linux)")

if __name__ == "__main__":
    main()