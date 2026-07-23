@echo off
setlocal

REM ================================================================
REM EDIT THESE VALUES
REM ================================================================
set "CTI_PATH=C:\Program Files\TOSHIBA TELI\TeliCamSDK\TeliCamApi\bin\x64\TeliCamTL64.cti"
set "SUBJECT=CALIBRATION"

REM Recommended starting values for 60-fps hand + monitor recording:
REM Exposure 8333 us = approximately 1/120 s
REM Gain 3 dB = modest amplification; improve room lighting before raising it
REM Gamma 1.0 and BlackLevel 0 preserve a natural tonal response
REM One-push WB requires a neutral white/gray card in the participant area

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
  --discard-frames-after-start 1 ^
  --manual ^
  --preview ^
  --preview-every 2 ^
  --codec MJPG

pause
