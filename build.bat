@echo off
rem Double-click to build "dist\RC505 Export Tool.exe". Pass -Clean to rebuild the venv from scratch.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1" %*
pause
