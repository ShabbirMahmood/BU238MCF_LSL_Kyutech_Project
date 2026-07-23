@echo off
setlocal
cd /d "%~dp0"

set /p SUBJECT=Enter Subject ID (example S01): 
if "%SUBJECT%"=="" set "SUBJECT=S00"

".venv\Scripts\python.exe" bu238mcf_lsl_30fps.py ^
  --settings camera_settings.json ^
  --mode ptb ^
  --subject "%SUBJECT%"

pause
