@echo off
setlocal
cd /d "%~dp0"

echo Checking Python 3.10...
py -3.10 --version >nul 2>&1
if errorlevel 1 (
  echo Python 3.10 was not found.
  echo Install 64-bit Python 3.10, then run this file again.
  pause
  exit /b 1
)

if not exist ".venv-sionna310\Scripts\python.exe" (
  echo Creating the local Python environment...
  py -3.10 -m venv .venv-sionna310
  if errorlevel 1 goto :failed
)

echo Installing AeroCity-RT and Sionna RT...
".venv-sionna310\Scripts\python.exe" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 goto :failed
".venv-sionna310\Scripts\python.exe" -m pip install -e ".[rt]"
if errorlevel 1 goto :failed

echo.
echo Installation completed.
echo Double-click scripts\StartVisualizer.bat to start the application.
pause
exit /b 0

:failed
echo.
echo Installation failed. Check the messages above and the network connection.
pause
exit /b 1
