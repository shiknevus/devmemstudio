@echo off
setlocal
cd /d "%~dp0"
if not exist "venv\Scripts\pythonw.exe" (
  echo ERROR: Project venv was not found. Use dist\DevmemStudio.exe instead.
  pause
  exit /b 1
)
start "" "venv\Scripts\pythonw.exe" "devmem_debug.py" %*
