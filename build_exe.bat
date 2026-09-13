@echo off
setlocal
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
  echo ERROR: Project venv\Scripts\python.exe was not found.
  pause
  exit /b 1
)
"venv\Scripts\python.exe" -m pip install -r requirements-lock.txt
if errorlevel 1 goto :failed
"venv\Scripts\python.exe" tools\make_icon.py
if errorlevel 1 goto :failed
"venv\Scripts\python.exe" -m unittest discover -s tests -v
if errorlevel 1 goto :failed
"venv\Scripts\python.exe" devmem_debug.py --offscreen --smoke-test artifacts\build-smoke
if errorlevel 1 goto :failed
"venv\Scripts\python.exe" -m PyInstaller --noconfirm DevmemStudio.spec
if errorlevel 1 goto :failed
"venv\Scripts\python.exe" tools\verify_exe.py
if errorlevel 1 goto :failed
"venv\Scripts\python.exe" tools\make_release.py
if errorlevel 1 goto :failed
echo.
echo Build complete: %CD%\dist\DevmemStudio.exe
exit /b 0
:failed
echo.
echo Build failed. See the error above.
pause
exit /b 1
