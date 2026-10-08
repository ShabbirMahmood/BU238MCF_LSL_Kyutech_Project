# RPS manual Toshiba Teli camera recorder

**Enter starts one recording. Q stops acquisition, drains the video queue, saves,
and exits.** The camera preview runs before Enter. Use the RPS camera program on
an operator/camera PC, not as a global keyboard hook for the participants' game.

This is an additive mode for `ShabbirMahmood/BU238MCF_LSL_Kyutech_Project`.
No existing Material Perception Python, BAT, JSON, MATLAB or recording file is edited.
The add-on imports the existing `bu238mcf_lsl_30fps.py` for Harvester availability,
GenTL BlockID access, raw image copying, and the repository's corrected Bayer-to-BGR
conversion. It does **not** call the parent's main routine, PTB controller, fixed-FPS
configuration routine, or writer, and does not modify the parent's constants.

## 1. Copy the files

Copy the ZIP's `run_RPS.bat` and **entire `RPS` folder** into the existing repository:

```text
BU238MCF_LSL_Kyutech_Project/
  .venv/                            Existing working environment
  bu238mcf_lsl_30fps.py              Existing file, unchanged
  camera_settings.json              Existing MP settings, unchanged
  requirements.txt                  Existing dependency pins
  run_manual_30fps.bat               Existing file, unchanged
  run_ptb_30fps.bat                  Existing file, unchanged
  run_RPS.bat                       NEW launcher
  RPS/                              NEW add-on folder
    rps_config.json                 Edit this configuration
    rps_recorder.py                 Main application and console/preview
    rps_camera.py                   Camera configuration and repo adapter
    rps_workers.py                  Acquisition and video writer workers
    rps_common.py                   Configuration/state/counting helpers
    test_rps.py                     Hardware-free tests
    requirements_RPS.txt            References ../requirements.txt
    README_RPS.md
    VALIDATION.txt
  Camera_Recordings/
    RPS/                            Created when running
```

Use your existing **working** `.venv`. Do not copy another PC's virtual environment.
The public repo includes an environment folder, but its portability is not assumed.
The BAT never installs or upgrades packages automatically.

From the repo root, only when the environment is missing:

```bat
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The inspected dependency file uses Harvester 1.4.3, genicam 1.5.1, pylsl 1.18.2,
NumPy >=1.26.4,<2.4 and opencv-python >=4.10,<5. The vendor TeliCamSDK/USB3 driver
and its CTI producer are separate installations. No SDK binaries, DLLs, `.venv`,
or FFmpeg executable are distributed in this add-on.

## 2. Edit RPS/rps_config.json

Do not edit the parent camera_settings.json for RPS. JSON must contain no comments
or trailing commas. A saved change takes effect on the next program launch; camera
parameters are fixed during each recording.

The included brightness and white-balance values follow the actual public
camera_settings.json read on 2026-10-08, not the differing example in its README:

```json
{
  "subject_id": "S01",
  "fps": 30.0,
  "width": 1920,
  "height": 1200,
  "exposure_us": 5000.0,
  "gain_db": 0.0,
  "gamma": 1.0,
  "black_level": 0.0,
  "balance_red": 2.09,
  "balance_blue": 2.45,
  "pixel_format": "BayerBG8"
}
```

This is a settings **excerpt**, not a replacement for the complete supplied JSON.
These are starting values from the repo, not a claim of optimal lighting/color on
another setup. Copy your locally tested values into this RPS JSON when they differ.

| Setting | Meaning |
|---|---|
| `subject_id` | Participant/pair ID; can be overridden with `--subject` |
| `cti_path` | Original installed `TeliCamTL64.cti`; do not copy it elsewhere |
| `serial` | `"1205610"` initially; empty string allowed only if exactly one camera exists |
| `output_dir` | Relative to this JSON: `../Camera_Recordings/RPS` means the main repo's output folder |
| `fps` | Requested acquisition rate; encoder rate uses camera readback |
| `width`, `height` | Sensor ROI size, not preview scaling; must meet camera limits/increments |
| `offset_x`, `offset_y` | ROI position; zero is the upper-left origin, not an automatic center crop |
| `exposure_us` | Fixed microsecond exposure; must be less than `1e6/fps` |
| `gain_db`, `gamma`, `black_level` | Fixed image parameters using the same nodes as the original recorder |
| `balance_red`, `balance_blue` | Fixed white balance; no auto/calibration file |
| `pixel_format` | An 8-bit Bayer format or Mono8; packed/10/12-bit/RGB formats are not added here |
| `camera_buffers` | GenTL acquisition buffer count; initial 128 |
| `writer_queue_frames` | Maximum queued RAW frames, initial 300; not a throughput fix |
| `warmup_seconds` | Preview/acquisition warm-up before Enter is enabled |
| `discard_frames_after_start` | Initial delivered frames skipped after Enter; default 1 |
| `post_stop_seconds` | Additional collection after Q; default **0**, unlike the parent's 2 seconds |
| `fetch_timeout_seconds` | Single fetch timeout; default 0.25 s |
| `camera_stall_timeout_seconds` | Stop on prolonged absence of frames; default 5 s |
| `preview_enabled`, `preview_width`, `preview_fps` | Preview on/off, maximum pixel width, UI update rate |
| `status_interval_seconds` | CMD reporting interval, initially one second |
| `queue_warning_fraction` | Warn at 75% queue occupancy, with hysteresis |
| `min_free_space_gb` | Refuse/start stopping when less free space remains; decimal GB |
| `writer_shutdown_timeout_seconds` | Queue drain timeout; a timeout marks failure, not successful recording |
| `video_backend` | `opencv` (default MJPG AVI) or optional `ffmpeg` (H.264 MKV) |
| `video_codec` | FourCC for OpenCV only, initially MJPG |
| `ffmpeg_path`, `ffmpeg_preset`, `ffmpeg_crf` | Used only for FFmpeg backend |
| `frame_stream_name`, `event_stream_name` | Defaults RPS_Camera_Frame_Metadata and RPS_Camera_Events |
| `require_lsl_consumer_before_start` | Default false; true blocks Enter without a frame inlet; a viewer is also a consumer |
| `lsl_linger_seconds` | Keep outlets alive briefly after final status; not a receipt acknowledgement |

The camera setter rejects out-of-range requests instead of silently clamping.
Minor floating-point camera quantization is allowed for FPS (within 2%); actual
values are printed/saved. Actual delivery can still be slower because of camera,
USB or PC bottlenecks. This is not a hard real-time controller.

At 1920x1200 Bayer8, 300 raw writer slots can hold about 691 MB. Adding 128 camera
slots gives about 986 MB raw-buffer capacity, excluding encoder, preview, Python,
driver and OS memory. Allocation/runtime behavior is vendor dependent.

## 3. Test before participant recording

Run from the repository root:

```bat
run_RPS.bat --self-test
run_RPS.bat --check
```

`--self-test` does not load a camera, create a preview or advertise LSL streams.
It checks start/stop timing gates, BlockIDs, queue overflow, simulated acquisition,
writer draining, failure reporting, CSV indexing and a full simulated application
run. It also performs real tiny OpenCV encode/decode tests and an FFmpeg test when
FFmpeg is available. Requires NumPy/OpenCV, which are in the original requirements.

`--check` imports the actual repository helpers and installed dependencies, checks
CTI file existence, loads liblsl, and encodes/decodes six synthetic frames with the
selected backend. It does not load the producer into Harvester or open the camera,
and therefore cannot prove the driver, camera nodes or live throughput work.

## 4. Record

1. Connect the camera and verify it in TeliViewer. Then close TeliViewer and other
   camera apps (including the other MP recorders).
2. Run `run_RPS.bat --subject RPS01` (or double-click to use the config subject).
3. Observe the preview. LSL outlets are advertised after camera setup. During
   warm-up/Ready no video frames are saved or sent as metadata samples.
4. Start LabRecorder and select both RPS camera streams, plus EEG and your PTB
   block-marker stream. Confirm LabRecorder is recording.
5. Click the camera preview or its CMD, then press **Enter** once.
6. Press **Q** once at the end. Q is not followed by Enter. The program stops
   collection, drains queued images and finalizes video/CSV/JSON before exiting.
7. Keep LabRecorder running until finalization has completed, then stop it.

The program does not ask for a blocking subject prompt: use the configuration or
`--subject`. Enter while recording is ignored. Q/Escape before recording cancels.
Escape, preview-window close, and Ctrl+C request a graceful stop. `H` prints help;
`P` pauses/resumes preview conversion without pausing recording. A repeated run
needs a new launch; no restart/pause recording state is added.

These are **focus-dependent keys**, not system-wide hotkeys. Pressing Q in your
separate RPS game/PTB window does not reliably control this camera app. Using a
separate operator/camera computer avoids interference with participant responses.
Avoid selecting text in the CMD window while recording: some Windows console
configurations pause applications/output while text is selected.

This mode does not listen to PTB codes 11/22, 900/999 or automatically cut camera
files at block boundaries. It records one operator-controlled session; your
existing PTB marker stream independently supplies block timing in XDF.

## 5. Buffer display and troubleshooting

Example only (not a hardware measurement):

```text
[Recording] 00:02:10 | Receive 30.0 FPS | Write 30.0 FPS | Frames 3900/3898 queued/written | Queue 1/300 (0%, ~0.03s) | ID gaps 0 | Timeouts 0
```

- Receive FPS counts completed fetched frames over the latest status interval.
- Write FPS counts video-write calls completed in the same interval.
- Frames shows cumulative accepted-for-writing / write calls returned.
- Queue shows waiting RAW frames, excluding a frame currently being encoded.
- Queue seconds is `queue_depth / FPS_readback`; it is **not measured network
  latency** or a measured age of each image.
- ID gaps count missing sequential camera BlockIDs during accepted recording
  frames. They do not identify whether the camera, USB link, SDK or host lost them.
- Camera buffer count is allocation, not measured SDK-buffer occupancy.
- Paused/slower preview deliberately omits display frames; this is independent of
  the recorded-frame queue and is not reported as recording loss.

A brief fluctuation is different from a queue that grows continuously. If it grows,
press P to reduce preview work; before the next run reduce FPS or ROI, use a fast
local SSD, close competing software, or test the optional FFmpeg encoder. A larger
queue only gives a slower writer more time before it overflows. It cannot sustain
writing slower than acquisition for an unlimited session.

At full queue the app records an error and stops collection; it does not silently
drop queued frames and claim success. The already accepted queue is drained when
possible. Camera stalls, non-increasing BlockIDs, low disk space, LSL failures and
writer failures are also reported. No program can guarantee zero dropped frames,
zero delay or recovery from a power/USB/OS/native-driver failure.

## 6. LSL schema and timing

`RPS_Camera_Frame_Metadata`: three double64 channels with the same meanings as the
parent project:

1. frame_id: camera/GenTL BlockID, not a fabricated counter.
2. video_frame_index: one-based intended frame position in the output file.
3. dropped_frame_total: cumulative inferred missing BlockIDs since recording began.

There is no duplicate timestamp channel. The LSL sample timestamp is taken from
`pylsl.local_clock()` immediately after the completed frame fetch, before copy,
colour conversion, preview and encoding. The timestamp is passed explicitly when
publishing and is kept with the packet through the writer.

**It is a host-delivery timestamp, NOT hardware exposure onset, camera oscillator
time or photon arrival time.** No arbitrary delay subtraction, clock calibration,
or unsupported camera buffer timestamp is introduced. A frame delivered after
Enter may have begun exposure before Enter. The initial one-frame discard does
not guarantee otherwise.

Unlike the parent's nominal 30 Hz declaration, this add-on declares frame metadata
as **irregular (nominal_srate=0)**. Actual camera FPS is stored as metadata. This
helps prevent importing software from treating jittered host deliveries and gaps
as an exact sample grid. Use the synchronized XDF sample timestamps for comparison
with EEG/PTB/Tobii; do not directly subtract another PC's unsynchronized clocks.

`RPS_Camera_Events`: string events for stream ready, acquisition running, ready for
Enter, recording start, first frame, stop request, anomalies and finalization.
Manual start/stop timestamps mark the time software handles the operator input,
not hardware keyboard closure or an external PTB action. No consumer check proves
that LabRecorder is saving data.

Frame metadata is emitted after acceptance into the writer queue. It is NOT a disk
ACK. A later writer failure can leave more XDF frame samples than usable file
frames. Use the summary and video/CSV checks before using the file for analysis.

## 7. Saved files

Each launch creates a unique subdirectory. Existing recordings are never replaced:

```text
Camera_Recordings/RPS/RPS_RPS01_<date_time_unique>/
  video.avi          Default OpenCV/MJPG
  frames.csv         Per-frame rows after the writer call returns
  events.csv         Camera events and host LSL timestamps
  config_used.json   Resolved settings for this run
  summary.json       Camera readback, software versions, counts, delays and status
```

For FFmpeg, the video is `video.mkv` and an additional `video.ffmpeg.log` is saved.
On exceptions, `error.txt` contains the traceback when available. A cancellation
before Enter removes only this run's empty video header; the audit files remain.

`frames.csv` columns are `frame_id`, `video_frame_index`, `host_lsl_timestamp_s`,
`host_interframe_ms`, `dropped_this_frame`, `dropped_frame_total`, and
`writer_return_lsl_s`. The first interval is blank because no prior frame exists.
`writer_return_lsl_s` is not disk persistence time or exposure time.

A clean software finish has matching accepted/write-call/CSV/LSL counts, no error,
and a finalized writer. `FinishedWithWarnings` records gaps/timeouts;
`FailedOrIncomplete` is not a successful recording. OpenCV does not return a
per-frame disk-write acknowledgement, so matching counts is necessary but not a
complete physical integrity guarantee. Check that the resulting video decodes.

AVI/MKV is encoded at the camera FPS readback using consecutive submitted frames.
Missing camera frames are NOT duplicated/padded. The ordinary player's
`frame_index / FPS` timeline may therefore differ from real elapsed time after
loss; use the recorded frame timestamps to align EEG, not video time alone.

Before a real 30-minute session, run a full-duration pilot with the actual lighting,
resolution, encoder, storage, preview and other applications active.

## 8. Optional smaller H.264 recording

Default MJPG uses the same type of writer as the public repository. If storage or
queue growth is a problem, install a separate FFmpeg build including `libx264`
and change:

```json
{
  "video_backend": "ffmpeg",
  "ffmpeg_path": "C:/ffmpeg/bin/ffmpeg.exe",
  "ffmpeg_preset": "ultrafast",
  "ffmpeg_crf": 20
}
```

Run `run_RPS.bat --check` again. This backend pipes BGR frames to a background
FFmpeg process and writes H.264/MKV. CRF controls lossy quality; smaller CRF means
higher quality/larger files. Faster encoding presets favor throughput. File size,
throughput and visual quality remain scene/hardware dependent. No automatic
encoder switching or changing FPS is performed during a recording.

## Sources inspected

Accessed 2026-10-08. The source helper SHA-256 is saved per run so your local version
is traceable. The public README's example settings are not identical to its JSON.

- https://github.com/ShabbirMahmood/BU238MCF_LSL_Kyutech_Project
- https://raw.githubusercontent.com/ShabbirMahmood/BU238MCF_LSL_Kyutech_Project/main/bu238mcf_lsl_30fps.py
- https://raw.githubusercontent.com/ShabbirMahmood/BU238MCF_LSL_Kyutech_Project/main/camera_settings.json
- https://raw.githubusercontent.com/ShabbirMahmood/BU238MCF_LSL_Kyutech_Project/main/requirements.txt
- https://harvesters.readthedocs.io/en/latest/TUTORIAL.html
- https://docs.opencv.org/4.x/d7/dfc/group__highgui.html
- https://docs.python.org/3/library/msvcrt.html
- https://labstreaminglayer.readthedocs.io/info/faqs.html
- https://ffmpeg.org/ffmpeg-codecs.html

Testing in the supplied environment used synthetic frames, fake camera/LSL objects,
and real small MJPG/H.264 encode/decode checks. No Toshiba camera, Windows CTI,
Windows keyboard/display, or LabRecorder was available there. See VALIDATION.txt.
