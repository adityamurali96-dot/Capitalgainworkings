@echo off
REM Double-click on Windows to launch cg-engine.
REM First run creates a virtual environment (.venv) and installs dependencies.
setlocal
cd /d "%~dp0"

REM Prefer the Python launcher (py), fall back to python on PATH.
set "PY=python"
where py >nul 2>nul && set "PY=py -3"

if not exist ".venv\Scripts\python.exe" (
  echo First run: creating virtual environment and installing dependencies...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo.
    echo Could not create the virtual environment.
    echo Install Python 3 from https://www.python.org/downloads/ and tick
    echo "Add python.exe to PATH", then run this file again.
    echo.
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Dependency install failed. Check your internet connection and retry.
    echo.
    pause
    exit /b 1
  )
)

echo Starting cg-engine - open http://127.0.0.1:5000
REM Open the browser shortly after the server has had time to start (no nested quotes).
start "" /b cmd /c "ping -n 3 127.0.0.1 >nul & explorer http://127.0.0.1:5000"
".venv\Scripts\python.exe" app.py

echo.
echo cg-engine stopped.
pause
