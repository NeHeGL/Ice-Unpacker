@echo off
setlocal
title Ice Unpacker

cd /d "%~dp0"

:: -- Make sure the virtual environment exists --------------------
if not exist ".venv\Scripts\python.exe" (
    echo  [INFO] Virtual environment not found. Running installer...
    echo.
    set ICE_AUTO_INSTALL=1
    call "%~dp0install.bat"
    set ICE_AUTO_INSTALL=
    if errorlevel 1 (
        echo  [ERROR] Installation failed. Fix the errors above and try again.
        pause
        exit /b 1
    )
)

echo.
echo Starting Ice Unpacker...
".venv\Scripts\python.exe" ice_unpacker.py %*
if errorlevel 1 (
    echo.
    echo ERROR: Ice Unpacker exited with an error. See above for details.
    pause
)
