@echo off
cd /d %~dp0
start "" http://localhost:8080
venv\Scripts\python app.py
pause
