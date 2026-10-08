@echo off
setlocal
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
  echo ERROR: Project venv\Scripts\python.exe was not found.
  pause
  exit /b 1
)
rem Steps, parallelism and logs (build\logs\*.log): see tools\build.py
"venv\Scripts\python.exe" tools\build.py
if errorlevel 1 goto :failed
exit /b 0
:failed
echo.
echo Build failed. See the error above.
pause
exit /b 1
