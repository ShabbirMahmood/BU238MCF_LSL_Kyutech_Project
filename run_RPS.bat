@echo off
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0"
if errorlevel 1 (
    echo [Error] Cannot enter the repository folder.
    pause
    exit /b 1
)
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [Error] No working Python environment at .venv\Scripts\python.exe
    echo Create the environment inside THIS repository with:
    echo   py -3.11 -m venv .venv
    echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo Install the Toshiba TeliCamSDK separately. Do not copy another PC's .venv.
    popd
    pause
    exit /b 1
)
if not exist "%~dp0RPS\rps_recorder.py" (
    echo [Error] Copy the entire RPS folder beside run_RPS.bat.
    popd
    pause
    exit /b 1
)
set "PYTHONUNBUFFERED=1"
set "PYTHONDONTWRITEBYTECODE=1"
echo [RPS Camera] Enter: Start ^| Q: Stop, Save And Exit ^| H: Help ^| P: Preview
"%PY%" -u "%~dp0RPS\rps_recorder.py" --config "%~dp0RPS\rps_config.json" %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo [Attention] The run reported an error. Keep the console output and session logs.
popd
pause
endlocal & exit /b %RC%
