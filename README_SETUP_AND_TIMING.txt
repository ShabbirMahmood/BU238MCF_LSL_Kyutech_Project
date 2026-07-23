BU238MCF 60-FPS LSL CAMERA PROJECT
=================================

FILES
-----
toshiba_teli_camera_lsl_60fps.py
requirements.txt
run_calibrate_BU238MCF_60fps.bat
run_ptb_BU238MCF_60fps_AutoWB.bat
run_ptb_BU238MCF_60fps_FixedWB.bat
Camera_Recordings/

RECOMMENDED IMAGE SETTINGS (STARTING POINT)
-------------------------------------------
Resolution     : 1920 x 1200 (camera default)
Frame rate     : 60 fps
Pixel format   : BayerBG8
Exposure       : 8333 us (~1/120 s)
Gain           : 3 dB
Gamma          : 1.0
Black level    : 0
White balance  : one-push during calibration, then fixed R/B ratios
Preview        : calibration only; OFF during EEG recording
Video codec    : MJPG AVI

These settings assume strong, flicker-free room lighting. Use two high-CRI
LED panels around 5000-5600 K, placed approximately 45 degrees to the
participant. Keep the monitor visible without clipping and keep the hand/
number pad adequately illuminated. Open the C-mount lens aperture before
raising gain. If the video remains dark, improve lighting first, then try
gain 6 dB. If hand motion is still blurred, reduce exposure to 6000-7000 us
and add more light.

LSL STREAMS
-----------
Camera_Frame_Metadata (4 channels):
  1 frame_id
  2 video_frame_index
  3 camera_timestamp_s
  4 dropped_frame_total

The per-sample XDF time_stamps are the host LSL times at completed-frame
delivery. They are intentionally not duplicated as a numeric channel.

Camera_Events:
  CAMERA_STREAM_READY
  CAMERA_ACQUISITION_RUNNING
  CAMERA_WAITING_FOR_PTB_900
  PTB_900_RECEIVED_BY_CAMERA_APP
  RECORDING_GATE_OPEN
  FIRST_RECORDED_FRAME_RECEIVED
  FIRST_RECORDED_FRAME_WRITTEN
  FRAME_DROP (only on anomaly)
  PTB_999_RECEIVED_BY_CAMERA_APP
  CAMERA_RECORDING_STOPPED

DELAY REPORT
------------
The command window prints:
  PTB source timestamp
  LSL time correction
  PTB timestamp mapped to camera-PC clock
  camera application receive time
  estimated delivery/scheduling delay
  recording-gate time
  first recorded frame receive/enqueue/write times
  stop and AVI-finalization times

OUTPUT
------
Camera_Recordings/<subject>_<date>_camera_60fps.avi
Camera_Recordings/<subject>_<date>_camera_frames.csv
Camera_Recordings/<subject>_<date>_camera_summary.json

INSTALL
-------
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

Then edit CTI_PATH in the BAT files.

WORKFLOW
--------
1. Confirm camera image in TeliViewer, then close TeliViewer.
2. Run run_calibrate_BU238MCF_60fps.bat and adjust lighting/lens.
3. Copy printed R/B white-balance ratios to the FixedWB BAT file.
4. Start EEG and Tobii outlets.
5. Run one PTB camera BAT file.
6. Run PTB; confirm PTB_Triggers connection.
7. Start LabRecorder after all outlets are visible.
8. Press the PTB start key. Code 900 opens the local recording gate.
9. Code 999 starts the post-stop buffer and finalization.
10. Stop LabRecorder last.

TIMING LIMIT
------------
The first Camera_Frame_Metadata XDF timestamp after code 900 measures
software end-to-end latency to completed-frame delivery, not physical
exposure onset. For physical timing, record the camera ExposureActive GPIO
through an isolated EEG AUX/digital input.
