@echo off
setlocal

REM ================================================================
REM EDIT THESE VALUES
REM ================================================================
set "CTI_PATH=C:\Program Files\TOSHIBA TELI\TeliCamSDK\TeliCamApi\bin\x64\TeliCamTL64.cti"
set "SUBJECT=S01"

REM Run the calibration BAT first. Replace SET_ME with the Red and Blue
REM BalanceRatio values printed by that calibration.
set "WB_RED=SET_ME"
set "WB_BLUE=SET_ME"

if /I "%WB_RED%"=="SET_ME" (
  echo ERROR: Set WB_RED to the calibrated Red BalanceRatio.
  pause
  exit /b 1
)
if /I "%WB_BLUE%"=="SET_ME" (
  echo ERROR: Set WB_BLUE to the calibrated Blue BalanceRatio.
  pause
  exit /b 1
)

REM Fixed-WB version: preferable when you need identical color settings
REM across participants under unchanged room lighting.

".venv\Scripts\python.exe" toshiba_teli_camera_lsl_60fps.py ^
  --cti "%CTI_PATH%" ^
  --output "Camera_Recordings" ^
  --subject "%SUBJECT%" ^
  --fps 60 ^
  --exposure-us 8333 ^
  --gain-db 3 ^
  --gamma 1.0 ^
  --black-level 0 ^
  --pixel-format BayerBG8 ^
  --fixed-white-balance ^
  --balance-red %WB_RED% ^
  --balance-blue %WB_BLUE% ^
  --warmup-seconds 2 ^
  --camera-buffers 128 ^
  --queue-size 120 ^
  --ptb-stream PTB_Triggers ^
  --ptb-start-code 900 ^
  --ptb-stop-code 999 ^
  --discard-frames-after-start 1 ^
  --post-stop-seconds 2 ^
  --codec MJPG ^
  --progress-every-frames 600

pause
