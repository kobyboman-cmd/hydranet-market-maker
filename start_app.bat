@echo off
setlocal enabledelayedexpansion
title Hydranet Pure DLMM Market Maker and Dashboard

:: Change working directory to project folder
set "HYDRA_DIR=%~dp0"
if defined HYDRA_DIR cd /d "%HYDRA_DIR%"

echo ========================================================================
echo           HYDRANET PURE DLMM MARKET MAKER ^& DASHBOARD
echo ========================================================================
echo.

:: Detect Python executable
set "PYTHON_EXE="
if exist "%HYDRA_DIR%venv\Scripts\python.exe" (
    set "PYTHON_EXE=%HYDRA_DIR%venv\Scripts\python.exe"
    echo [OK] Virtual environment detected: venv
)
if not defined PYTHON_EXE if exist "%HYDRA_DIR%.venv\Scripts\python.exe" (
    set "PYTHON_EXE=%HYDRA_DIR%.venv\Scripts\python.exe"
    echo [OK] Virtual environment detected: .venv
)
if not defined PYTHON_EXE if exist "C:\Python314\python.exe" (
    set "PYTHON_EXE=C:\Python314\python.exe"
    echo [OK] Using Python 3.14: C:\Python314\python.exe
)
if not defined PYTHON_EXE (
    where python >nul 2>nul
    if !errorlevel! equ 0 (
        set "PYTHON_EXE=python"
        echo [INFO] Using system Python on PATH
    ) else (
        echo [ERROR] Python was not found!
        echo Please ensure Python is installed and added to PATH.
        echo.
        pause
        exit /b 1
    )
)
echo.

:: If command-line arguments were provided, pass them directly to agent.py
if not "%~1"=="" (
    echo [INFO] Running agent.py with arguments: %*
    start "" cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:8080"
    "%PYTHON_EXE%" agent.py %*
    goto :end
)

:: Interactive Quick-Launch Menu for double-click execution
echo Select an option to launch:
echo.
echo   [1] Start DLMM Bot and Web Dashboard (Dry-Run / Simulation) [Default]
echo   [2] Start DLMM Bot and Web Dashboard (LIVE Trading)
echo   [3] Cashu Ecash Status ^& Balance
echo   [4] Export Cashu Ecash Token (for Minibits / eCash App withdrawal)
echo   [5] Open Web Dashboard in Browser (http://localhost:8080)
echo.

choice /c 12345 /t 8 /d 1 /m "Select option (1-5, auto-starts [1] in 8s):"
set "CHOICE_VAL=!errorlevel!"

echo.
if "!CHOICE_VAL!"=="1" (
    echo [INFO] Starting Pure DLMM Market Maker in DRY-RUN mode...
    echo [INFO] Embedded Web Dashboard will be available at http://localhost:8080
    start "" cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:8080"
    "%PYTHON_EXE%" agent.py --dry-run
)
if "!CHOICE_VAL!"=="2" (
    echo ========================================================================
    echo [WARNING] LIVE TRADING MODE PLACES REAL ORDERS ON STATE CHANNELS.
    echo ========================================================================
    choice /c YN /m "Are you sure you want to proceed with LIVE trading?"
    if !errorlevel! equ 1 (
        echo [INFO] Starting Pure DLMM Market Maker in LIVE mode...
        start "" cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:8080"
        "%PYTHON_EXE%" agent.py --live
    ) else (
        echo [INFO] Live trading canceled by user.
    )
)
if "!CHOICE_VAL!"=="3" (
    echo [INFO] Querying Cashu Ecash vault status...
    "%PYTHON_EXE%" agent.py --cashu-status
)
if "!CHOICE_VAL!"=="4" (
    echo [INFO] Exporting Cashu Ecash token...
    "%PYTHON_EXE%" agent.py --cashu-export
)
if "!CHOICE_VAL!"=="5" (
    echo [INFO] Opening Web Dashboard at http://127.0.0.1:8080 ...
    start http://127.0.0.1:8080
)

:end
if !errorlevel! neq 0 (
    echo.
    echo [!] Process exited with return code !errorlevel!.
)
echo.
echo Press any key to close this window...
pause >nul
