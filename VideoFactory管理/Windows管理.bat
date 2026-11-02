@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -B "%~dp0windows_manager.py"
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python 3 is required. Install Python with tkinter, then reopen this file.
    pause
    exit /b 1
  )
  python -B "%~dp0windows_manager.py"
)
if errorlevel 1 pause
endlocal
