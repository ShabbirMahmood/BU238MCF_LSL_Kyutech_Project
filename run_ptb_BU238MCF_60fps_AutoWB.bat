@echo off
setlocal

REM ================================================================
REM EDIT THESE VALUES
REM ================================================================
set "CTI_PATH=C:\Program Files\TOSHIBA TELI\TeliCamSDK\TeliCamApi\bin\x64\TeliCamTL64.cti"
set "SUBJECT=S01"

REM Immediate-use version: performs one-push white balance at camera startup.
REM Put a neutral gray/white card in the participant area during the warm-up.

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
  --white-balance-once ^
  --warmup-seconds 2 ^
  --white-balance-settle-seconds 1 ^
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
