@echo off
cd /d "E:\My_apps\Finnancial tool"
start "Trading Desk Server" .venv\Scripts\python.exe -m uvicorn app.main:app --reload
timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:8000
