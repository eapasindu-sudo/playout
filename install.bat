@echo off
REM One-time setup. Needs Python 3.11+ from python.org (tick "Add python.exe to PATH").
cd /d %~dp0
python -m venv venv || goto :err
venv\Scripts\python -m pip install --upgrade pip
venv\Scripts\python -m pip install -r requirements.txt || goto :err
echo.
echo Done. Run start.bat to start the playout.
pause
exit /b 0
:err
echo Setup failed. Check that Python is installed and on PATH.
pause
exit /b 1
