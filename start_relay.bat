@echo off
REM ComfyUI Mobile Relay launcher
REM Edit the path below if you place the project somewhere else.
cd /d %~dp0
uvicorn relay_server:app --host 0.0.0.0 --port 8080
pause
