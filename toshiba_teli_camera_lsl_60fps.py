#!/usr/bin/env python3
"""
toshiba_teli_camera_lsl_60fps.py

Toshiba Teli BU238MCF experiment camera recorder for an LSL/EEG setup.

Main design
-----------
* The camera is configured for 1920 x 1200 BayerBG8 at 60 fps.
* Camera acquisition starts before PTB trigger 900, so the pipeline is warm.
* Frames are discarded until PTB_Triggers sends 900.
* The first recorded frame is the first complete frame after a configurable
  safety discard (default: one frame) following trigger 900.
* PTB trigger 999 requests stop. The camera records a short post-session
  buffer and then closes the AVI and CSV cleanly.
* The actual video is saved locally; only compact metadata is sent through LSL.

LSL streams
-----------
1) Camera_Frame_Metadata (double64, 4 channels)
   - frame_id              : camera/GenTL BlockID
   - video_frame_index     : 1-based frame index in the local AVI
   - camera_timestamp_s    : camera/GenTL timestamp in camera clock
   - dropped_frame_total   : cumulative missing BlockIDs during recording

   IMPORTANT: The XDF time_stamps of this stream are the camera-PC LSL times
   sampled immediately after a completed frame buffer is delivered to Python.
   This host-delivery timestamp is the timestamp to use for software alignment
   with EEG/PTB/Tobii after XDF clock synchronization.

2) Camera_Events (string markers)
   Essential events only: ready, acquisition running, PTB 900 received,
   recording gate opened, first frame received/written, frame loss, PTB 999
   received, and recording stopped.

Timing interpretation
---------------------
The script prints and saves a step-by-step delay decomposition:
* PTB marker remote/source timestamp
* LSL inlet time correction
* PTB marker mapped into the camera-PC LSL clock
* marker arrival time in the camera application
* estimated LSL delivery/scheduling delay
* recording-gate opening time
* first recorded frame delivery time
* first frame enqueue and actual video-write time

These are software timing measurements. For physical exposure-onset timing,
record the camera's ExposureActive GPIO through an isolated EEG AUX/digital
input. The frame-buffer timestamp is not silently claimed to be exposure start.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import queue
import signal
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from genicam.gentl import TimeoutException
from harvesters.core import Harvester
from pylsl import (
    StreamInfo,
    StreamInlet,
    StreamOutlet,
    local_clock,
    resolve_byprop,
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class Config:
    cti_path: Path
    output_dir: Path
    subject: str
    serial: Optional[str]

    fps: float
    exposure_us: float
    gain_db: float
    gamma: float
    black_level: float
    pixel_format: str

    white_balance_once: bool
    balance_red: Optional[float]
    balance_blue: Optional[float]
    warmup_seconds: float
    white_balance_settle_seconds: float

    queue_size: int
    num_camera_buffers: int
    preview: bool
    preview_every: int

    auto_ptb: bool
    ptb_stream_name: str
    ptb_start_code: str
    ptb_stop_code: str
    discard_frames_after_start: int
    post_stop_seconds: float

    fetch_timeout_s: float
    video_codec: str
    progress_every_frames: int


# ---------------------------------------------------------------------------
# GenICam helpers
# ---------------------------------------------------------------------------

def safe_set_enum(node_map, name: str, value: str, required: bool = False) -> bool:
    node = getattr(node_map, name, None)
    if node is None:
        if required:
            raise RuntimeError(f"Required camera enumeration node is missing: {name}")
        print(f"[WARN] Camera node not available: {name}")
        return False
    try:
        node.value = value
        print(f"[CAMERA] {name} = {value}")
        return True
    except Exception as exc:
        if required:
            raise RuntimeError(f"Could not set required node {name}={value}: {exc}") from exc
        print(f"[WARN] Could not set {name}={value}: {exc}")
        return False


def safe_set_bool(node_map, name: str, value: bool) -> bool:
    node = getattr(node_map, name, None)
    if node is None:
        print(f"[WARN] Camera node not available: {name}")
        return False
    try:
        node.value = bool(value)
        print(f"[CAMERA] {name} = {bool(value)}")
        return True
    except Exception as exc:
        print(f"[WARN] Could not set {name}={value}: {exc}")
        return False


def safe_set_number(
    node_map,
    name: str,
    value: float,
    required: bool = False,
) -> Optional[float]:
    node = getattr(node_map, name, None)
    if node is None:
        if required:
            raise RuntimeError(f"Required numeric camera node is missing: {name}")
        print(f"[WARN] Camera node not available: {name}")
        return None
    try:
        target = float(value)
        try:
            target = max(float(node.min), min(float(node.max), target))
        except Exception:
            pass
        node.value = target
        actual = float(node.value)
        print(f"[CAMERA] {name} = {actual}")
        return actual
    except Exception as exc:
        if required:
            raise RuntimeError(f"Could not set required node {name}={value}: {exc}") from exc
        print(f"[WARN] Could not set {name}={value}: {exc}")
        return None


def safe_get_value(node_map, name: str, default=None):
    node = getattr(node_map, name, None)
    if node is None:
        return default
    try:
        return node.value
    except Exception:
        return default


def get_balance_ratio(node_map, selector: str) -> Optional[float]:
    try:
        node_map.BalanceRatioSelector.value = selector
        return float(node_map.BalanceRatio.value)
    except Exception:
        return None


def set_manual_balance_ratio(node_map, selector: str, value: float) -> float:
    safe_set_enum(node_map, "BalanceWhiteAuto", "Off")
    safe_set_enum(node_map, "BalanceRatioSelector", selector, required=True)
    actual = safe_set_number(node_map, "BalanceRatio", value, required=True)
    assert actual is not None
    return actual


def require_camera_frame_id(buffer) -> int:
    try:
        return int(buffer.frame_id)
    except Exception as exc:
        raise RuntimeError(
            "The GenTL producer did not expose buffer.frame_id (BlockID). "
            "This program requires the camera/transport frame ID so that "
            "dropped frames can be detected."
        ) from exc


def require_camera_timestamp_s(buffer) -> float:
    """
    Require a valid camera/GenTL buffer timestamp.

    The timestamp is retained in the camera clock domain. It is useful for
    inter-frame timing and drift QC; it is not directly an LSL timestamp.
    """
    try:
        timestamp_ns = int(buffer.timestamp_ns)
        if timestamp_ns > 0:
            return timestamp_ns * 1e-9
    except Exception:
        pass

    try:
        ticks = int(buffer.timestamp)
        frequency = float(buffer.timestamp_frequency)
        if ticks >= 0 and frequency > 0:
            return ticks / frequency
    except Exception:
        pass

    raise RuntimeError(
        "The GenTL producer did not expose a valid camera buffer timestamp. "
        "A camera timestamp is required by this streamlined metadata format."
    )


def copy_raw_component(component) -> tuple[np.ndarray, str]:
    """Copy raw Mono/Bayer data while the GenTL buffer is still valid."""
    width = int(component.width)
    height = int(component.height)
    fmt = str(component.data_format)
    data = component.data
    if data is None:
        raise RuntimeError("Camera returned an empty image component.")

    arr = np.asarray(data)
    if arr.size != width * height:
        raise RuntimeError(
            f"Unexpected component size for {fmt}: got {arr.size}, "
            f"expected {width * height}."
        )
    return arr.reshape(height, width).astype(np.uint8, copy=True), fmt


def raw_to_bgr(raw: np.ndarray, pixel_format: str) -> np.ndarray:
    conversions = {
        "Mono8": cv2.COLOR_GRAY2BGR,
        "BayerRG8": cv2.COLOR_BAYER_RG2BGR,
        "BayerGR8": cv2.COLOR_BAYER_GR2BGR,
        "BayerBG8": cv2.COLOR_BAYER_BG2BGR,
        "BayerGB8": cv2.COLOR_BAYER_GB2BGR,
    }
    if pixel_format not in conversions:
        raise RuntimeError(
            f"Unsupported pixel format {pixel_format!r}. "
            "BU238MCF factory format is normally BayerBG8; confirm it in "
            "TeliViewer."
        )
    return cv2.cvtColor(raw, conversions[pixel_format])


# ---------------------------------------------------------------------------
# LSL streams
# ---------------------------------------------------------------------------

def make_lsl_outlets(source_id: str, nominal_fps: float):
    """
    Create only the two streams needed for this experiment.

    Camera_Frame_Metadata uses the LSL sample timestamp as host frame-delivery
    time. Therefore host_lsl_time is not duplicated as another numeric channel.
    """
    labels_units = [
        ("frame_id", "count"),
        ("video_frame_index", "count"),
        ("camera_timestamp_s", "seconds"),
        ("dropped_frame_total", "count"),
    ]

    info = StreamInfo(
        "Camera_Frame_Metadata",
        "VideoFrameMetadata",
        len(labels_units),
        float(nominal_fps),
        "double64",
        source_id,
    )
    channels = info.desc().append_child("channels")
    for label, unit in labels_units:
        ch = channels.append_child("channel")
        ch.append_child_value("label", label)
        ch.append_child_value("unit", unit)
        ch.append_child_value("type", "CameraMetadata")

    info.desc().append_child_value(
        "timestamp_meaning",
        "XDF time_stamps are camera-PC LSL times sampled immediately after "
        "a completed frame buffer is delivered to Python.",
    )
    info.desc().append_child_value(
        "camera_timestamp_meaning",
        "camera_timestamp_s is in the GenTL/camera clock domain and must not "
        "be directly subtracted from another LSL stream.",
    )

    metadata_outlet = StreamOutlet(info, chunk_size=1, max_buffered=360)

    event_info = StreamInfo(
        "Camera_Events",
        "Markers",
        1,
        0.0,
        "string",
        source_id + "_events",
    )
    event_outlet = StreamOutlet(event_info)

    return metadata_outlet, event_outlet


def push_event(outlet: StreamOutlet, text: str, timestamp: Optional[float] = None):
    ts = float(local_clock()) if timestamp is None else float(timestamp)
    outlet.push_sample([text], timestamp=ts)
    print(f"[CAMERA EVENT] {text} | camera_local_lsl={ts:.6f}")


def normalize_marker_code(value) -> str:
    text = str(value).strip()
    try:
        number = float(text)
        if math.isfinite(number) and number.is_integer():
            return str(int(number))
    except (TypeError, ValueError):
        pass
    return text


class PTBController(threading.Thread):
    """Receive PTB 900/999 and map marker timestamps to camera-PC LSL time."""

    def __init__(
        self,
        stream_name: str,
        start_code: str,
        stop_code: str,
        shutdown_event: threading.Event,
    ):
        super().__init__(daemon=True)
        self.stream_name = stream_name
        self.start_code = str(start_code)
        self.stop_code = str(stop_code)
        self.shutdown_event = shutdown_event

        self.connected = threading.Event()
        self.record_start = threading.Event()
        self.record_stop = threading.Event()

        self.start_remote_timestamp: Optional[float] = None
        self.start_time_correction_s: Optional[float] = None
        self.start_camera_local_estimate: Optional[float] = None
        self.start_received_camera_local: Optional[float] = None

        self.stop_remote_timestamp: Optional[float] = None
        self.stop_time_correction_s: Optional[float] = None
        self.stop_camera_local_estimate: Optional[float] = None
        self.stop_received_camera_local: Optional[float] = None

        self.error: Optional[BaseException] = None

    @staticmethod
    def _time_correction(inlet: StreamInlet) -> float:
        try:
            return float(inlet.time_correction(timeout=5.0))
        except TypeError:
            return float(inlet.time_correction(5.0))

    def _print_marker_delay(
        self,
        phase: str,
        code: str,
        remote_ts: float,
        correction_s: float,
        local_est: float,
        local_rx: float,
    ):
        delivery_ms = (local_rx - local_est) * 1000.0
        print()
        print("=" * 76)
        print(f"[DELAY {phase}] PTB code {code} received by camera application")
        print(f"  1. PTB source/remote timestamp       : {remote_ts:.9f} s")
        print(f"  2. LSL inlet time correction         : {correction_s * 1000.0:.3f} ms")
        print(f"  3. Marker in camera-PC LSL clock     : {local_est:.9f} s")
        print(f"  4. Camera application receive time   : {local_rx:.9f} s")
        print(f"  5. Estimated LSL delivery/scheduling : {delivery_ms:.3f} ms")
        print("=" * 76)

    def run(self) -> None:
        try:
            print(f"[PTB] Waiting for LSL stream {self.stream_name!r} ...")
            found = []
            while not self.shutdown_event.is_set() and not found:
                found = resolve_byprop(
                    "name", self.stream_name, minimum=1, timeout=1.0
                )
            if not found:
                return

            inlet = StreamInlet(found[0], max_buflen=60, recover=True)
            initial_correction = self._time_correction(inlet)
            self.connected.set()
            print(
                f"[PTB] Connected to {self.stream_name!r}; "
                f"initial time correction={initial_correction * 1000.0:.3f} ms."
            )

            while not self.shutdown_event.is_set():
                sample, remote_ts = inlet.pull_sample(timeout=0.2)
                if sample is None:
                    continue

                code = normalize_marker_code(sample[0])
                if code not in {self.start_code, self.stop_code}:
                    continue

                local_rx = float(local_clock())
                correction_s = self._time_correction(inlet)
                local_est = float(remote_ts) + correction_s

                if code == self.start_code and not self.record_start.is_set():
                    self.start_remote_timestamp = float(remote_ts)
                    self.start_time_correction_s = correction_s
                    self.start_camera_local_estimate = local_est
                    self.start_received_camera_local = local_rx
                    self._print_marker_delay(
                        "START", code, float(remote_ts), correction_s,
                        local_est, local_rx
                    )
                    self.record_start.set()

                elif code == self.stop_code and not self.record_stop.is_set():
                    self.stop_remote_timestamp = float(remote_ts)
                    self.stop_time_correction_s = correction_s
                    self.stop_camera_local_estimate = local_est
                    self.stop_received_camera_local = local_rx
                    self._print_marker_delay(
                        "STOP", code, float(remote_ts), correction_s,
                        local_est, local_rx
                    )
                    self.record_stop.set()

        except BaseException as exc:
            self.error = exc
            self.shutdown_event.set()


# ---------------------------------------------------------------------------
# Video writer
# ---------------------------------------------------------------------------

@dataclass
class FramePacket:
    video_frame_index: int
    frame_id: int
    raw: np.ndarray
    pixel_format: str


class VideoWriterWorker(threading.Thread):
    def __init__(
        self,
        output_path: Path,
        fps: float,
        codec: str,
        frame_queue: queue.Queue,
        stop_event: threading.Event,
    ):
        super().__init__(daemon=True)
        self.output_path = output_path
        self.fps = float(fps)
        self.codec = codec
        self.frame_queue = frame_queue
        self.stop_event = stop_event
        self.writer: Optional[cv2.VideoWriter] = None
        self.frames_written = 0
        self.first_frame_written_lsl: Optional[float] = None
        self.first_frame_written_index: Optional[int] = None
        self.error: Optional[BaseException] = None

    def _open(self, bgr: np.ndarray) -> None:
        height, width = bgr.shape[:2]
        fourcc = cv2.VideoWriter_fourcc(*self.codec)
        self.writer = cv2.VideoWriter(
            str(self.output_path),
            fourcc,
            self.fps,
            (width, height),
            True,
        )
        if not self.writer.isOpened():
            raise RuntimeError(
                f"Could not open VideoWriter for {self.output_path}. "
                "Use MJPG with an AVI output on Windows."
            )

    def run(self) -> None:
        try:
            while not self.stop_event.is_set() or not self.frame_queue.empty():
                try:
                    packet: FramePacket = self.frame_queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                bgr = raw_to_bgr(packet.raw, packet.pixel_format)
                if self.writer is None:
                    self._open(bgr)

                self.writer.write(bgr)
                self.frames_written += 1

                if self.first_frame_written_lsl is None:
                    self.first_frame_written_lsl = float(local_clock())
                    self.first_frame_written_index = packet.video_frame_index

                self.frame_queue.task_done()

        except BaseException as exc:
            self.error = exc
        finally:
            if self.writer is not None:
                self.writer.release()


# ---------------------------------------------------------------------------
# Camera configuration and white balance
# ---------------------------------------------------------------------------

def configure_camera(ia, cfg: Config) -> dict:
    nm = ia.remote_device.node_map

    safe_set_enum(nm, "AcquisitionMode", "Continuous", required=True)
    safe_set_enum(nm, "TriggerMode", "Off", required=True)
    safe_set_enum(nm, "PixelFormat", cfg.pixel_format, required=True)

    safe_set_bool(nm, "AcquisitionFrameRateEnable", True)
    actual_fps = safe_set_number(
        nm, "AcquisitionFrameRate", cfg.fps, required=True
    )

    safe_set_enum(nm, "ExposureTimeControl", "Manual")
    actual_exposure = safe_set_number(
        nm, "ExposureTime", cfg.exposure_us, required=True
    )
    actual_gain = safe_set_number(nm, "Gain", cfg.gain_db, required=True)
    actual_black = safe_set_number(
        nm, "BlackLevel", cfg.black_level, required=True
    )
    actual_gamma = safe_set_number(
        nm, "Gamma", cfg.gamma, required=True
    )

    actual_red = None
    actual_blue = None
    if cfg.balance_red is not None and cfg.balance_blue is not None:
        actual_red = set_manual_balance_ratio(
            nm, "Red", cfg.balance_red
        )
        actual_blue = set_manual_balance_ratio(
            nm, "Blue", cfg.balance_blue
        )

    width = int(safe_get_value(nm, "Width", 0))
    height = int(safe_get_value(nm, "Height", 0))
    model = str(safe_get_value(nm, "DeviceModelName", "unknown"))
    serial = str(safe_get_value(nm, "DeviceSerialNumber", "unknown"))

    try:
        ia.num_buffers = max(
            int(cfg.num_camera_buffers), int(ia.min_num_buffers)
        )
        print(f"[CAMERA] Host acquisition buffers = {ia.num_buffers}")
    except Exception as exc:
        print(f"[WARN] Could not set ia.num_buffers: {exc}")

    return {
        "model": model,
        "serial": serial,
        "width": width,
        "height": height,
        "pixel_format": cfg.pixel_format,
        "requested_fps": cfg.fps,
        "actual_fps": actual_fps,
        "requested_exposure_us": cfg.exposure_us,
        "actual_exposure_us": actual_exposure,
        "requested_gain_db": cfg.gain_db,
        "actual_gain_db": actual_gain,
        "requested_black_level": cfg.black_level,
        "actual_black_level": actual_black,
        "requested_gamma": cfg.gamma,
        "actual_gamma": actual_gamma,
        "manual_balance_red": actual_red,
        "manual_balance_blue": actual_blue,
    }


def begin_one_push_white_balance(ia) -> bool:
    nm = ia.remote_device.node_map
    return safe_set_enum(nm, "BalanceWhiteAuto", "Once")


def finish_one_push_white_balance(ia) -> tuple[Optional[float], Optional[float]]:
    nm = ia.remote_device.node_map
    safe_set_enum(nm, "BalanceWhiteAuto", "Off")
    red = get_balance_ratio(nm, "Red")
    blue = get_balance_ratio(nm, "Blue")
    print(
        "[CAMERA] Fixed white-balance ratios after one-push: "
        f"Red={red if red is not None else 'unavailable'}, "
        f"Blue={blue if blue is not None else 'unavailable'}"
    )
    return red, blue


# ---------------------------------------------------------------------------
# Timing reporting
# ---------------------------------------------------------------------------

def add_if_value(d: dict, key: str, value):
    if value is not None:
        if isinstance(value, float) and not math.isfinite(value):
            return
        d[key] = value


def print_start_delay_summary(
    ptb: Optional[PTBController],
    gate_open_lsl: float,
    first_frame_received_lsl: float,
    first_frame_enqueued_lsl: float,
    first_frame_camera_s: float,
    first_frame_id: int,
    writer: VideoWriterWorker,
):
    print()
    print("#" * 76)
    print("[START DELAY SUMMARY]")
    if ptb is not None and ptb.start_camera_local_estimate is not None:
        marker_local = ptb.start_camera_local_estimate
        local_rx = ptb.start_received_camera_local
        print(
            f"  PTB 900 mapped to camera clock       : {marker_local:.9f} s"
        )
        print(
            f"  Camera app received PTB 900          : {local_rx:.9f} s"
        )
        print(
            f"  900 -> camera app receive            : "
            f"{(local_rx - marker_local) * 1000.0:.3f} ms"
        )
        print(
            f"  900 -> recording gate open           : "
            f"{(gate_open_lsl - marker_local) * 1000.0:.3f} ms"
        )
        print(
            f"  900 -> first recorded frame received : "
            f"{(first_frame_received_lsl - marker_local) * 1000.0:.3f} ms"
        )
        print(
            f"  900 -> first frame enqueued           : "
            f"{(first_frame_enqueued_lsl - marker_local) * 1000.0:.3f} ms"
        )
    print(
        f"  Gate -> first recorded frame received: "
        f"{(first_frame_received_lsl - gate_open_lsl) * 1000.0:.3f} ms"
    )
    print(
        f"  First recorded camera frame ID       : {first_frame_id}"
    )
    print(
        f"  First camera timestamp (camera clock): {first_frame_camera_s:.9f} s"
    )
    if writer.first_frame_written_lsl is not None:
        print(
            f"  First frame receive -> AVI write      : "
            f"{(writer.first_frame_written_lsl - first_frame_received_lsl) * 1000.0:.3f} ms"
        )
        if ptb is not None and ptb.start_camera_local_estimate is not None:
            print(
                f"  900 -> first frame actually written   : "
                f"{(writer.first_frame_written_lsl - ptb.start_camera_local_estimate) * 1000.0:.3f} ms"
            )
    print("#" * 76)


# ---------------------------------------------------------------------------
# Main acquisition
# ---------------------------------------------------------------------------

def run(cfg: Config) -> int:
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    if not cfg.cti_path.is_file():
        raise FileNotFoundError(
            f"GenTL CTI file does not exist:\n{cfg.cti_path}"
        )

    run_stamp = time.strftime("%Y%m%d_%H%M%S")
    base = f"{cfg.subject}_{run_stamp}"
    video_path = cfg.output_dir / f"{base}_camera_60fps.avi"
    csv_path = cfg.output_dir / f"{base}_camera_frames.csv"
    summary_path = cfg.output_dir / f"{base}_camera_summary.json"

    shutdown_event = threading.Event()
    writer_stop_event = threading.Event()
    frame_queue: queue.Queue = queue.Queue(maxsize=cfg.queue_size)

    def request_shutdown(signum=None, frame=None):
        del signum, frame
        shutdown_event.set()

    signal.signal(signal.SIGINT, request_shutdown)
    try:
        signal.signal(signal.SIGTERM, request_shutdown)
    except Exception:
        pass

    h = Harvester()
    ia = None
    metadata_outlet = None
    event_outlet = None
    ptb: Optional[PTBController] = None
    writer: Optional[VideoWriterWorker] = None
    csv_file = None

    summary: dict = {
        "subject": cfg.subject,
        "start_wall_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cti_path": str(cfg.cti_path),
        "video_path": str(video_path),
        "frame_csv_path": str(csv_path),
        "configured_parameters": {
            k: str(v) if isinstance(v, Path) else v
            for k, v in asdict(cfg).items()
        },
    }

    # Start-timing values.
    gate_open_lsl: Optional[float] = None
    first_frame_received_lsl: Optional[float] = None
    first_frame_enqueued_lsl: Optional[float] = None
    first_frame_camera_s: Optional[float] = None
    first_frame_id: Optional[int] = None
    start_summary_printed = False

    try:
        print(f"[SDK] Loading Toshiba GenTL producer:\n{cfg.cti_path}")
        h.add_file(str(cfg.cti_path))
        h.update()

        if not h.device_info_list:
            raise RuntimeError(
                "No camera detected. Verify the BU238MCF in TeliViewer, "
                "close TeliViewer, then run this program."
            )

        print("[SDK] Detected devices:")
        for index, device in enumerate(h.device_info_list):
            print(f"  [{index}] {device}")

        ia = (
            h.create({"serial_number": cfg.serial})
            if cfg.serial else h.create(0)
        )

        camera_info = configure_camera(ia, cfg)
        summary["camera"] = camera_info

        source_id = (
            f"ToshibaTeli_{camera_info['model']}_{camera_info['serial']}"
        )
        metadata_outlet, event_outlet = make_lsl_outlets(
            source_id, cfg.fps
        )
        push_event(event_outlet, "CAMERA_STREAM_READY")
        print(
            "[LSL] Visible streams: Camera_Frame_Metadata and Camera_Events."
        )

        if cfg.auto_ptb:
            ptb = PTBController(
                cfg.ptb_stream_name,
                cfg.ptb_start_code,
                cfg.ptb_stop_code,
                shutdown_event,
            )
            ptb.start()
        else:
            print("[CONTROL] Manual mode selected.")

        # Open CSV and start writer before trigger 900 to reduce startup work.
        csv_file = csv_path.open("w", newline="", encoding="utf-8")
        fieldnames = [
            "frame_id",
            "video_frame_index",
            "camera_timestamp_s",
            "host_lsl_timestamp_s",
            "camera_interframe_ms",
            "host_interframe_ms",
            "dropped_this_frame",
            "dropped_frame_total",
        ]
        csv_writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        csv_writer.writeheader()

        writer = VideoWriterWorker(
            video_path,
            cfg.fps,
            cfg.video_codec,
            frame_queue,
            writer_stop_event,
        )
        writer.start()

        # Warm camera before 900. This removes acquisition-start delay from the
        # experiment recording and makes the first saved frame predictable.
        acquisition_start_command_lsl = float(local_clock())
        print()
        print("=" * 76)
        print("[CAMERA PIPELINE START]")
        print(
            f"  1. ia.start() command issued          : "
            f"{acquisition_start_command_lsl:.9f} s"
        )
        ia.start()
        acquisition_start_return_lsl = float(local_clock())
        print(
            f"  2. ia.start() returned                : "
            f"{acquisition_start_return_lsl:.9f} s"
        )
        print(
            f"  3. ia.start() call duration           : "
            f"{(acquisition_start_return_lsl - acquisition_start_command_lsl) * 1000.0:.3f} ms"
        )
        print("=" * 76)
        push_event(
            event_outlet,
            "CAMERA_ACQUISITION_RUNNING",
            acquisition_start_return_lsl,
        )

        if cfg.white_balance_once:
            print(
                "[WHITE BALANCE] Place a neutral white/gray card in the "
                "participant region during warm-up."
            )
            begin_one_push_white_balance(ia)

        warmup_end = local_clock() + cfg.warmup_seconds
        first_warmup_frame_lsl: Optional[float] = None
        warmup_frames = 0

        while not shutdown_event.is_set() and local_clock() < warmup_end:
            try:
                with ia.fetch(timeout=cfg.fetch_timeout_s) as buffer:
                    host_lsl = float(local_clock())
                    if first_warmup_frame_lsl is None:
                        first_warmup_frame_lsl = host_lsl
                        print(
                            f"[CAMERA PIPELINE] First warm-up frame delivered: "
                            f"{host_lsl:.9f} s; "
                            f"start-return -> first frame="
                            f"{(host_lsl - acquisition_start_return_lsl) * 1000.0:.3f} ms"
                        )
                    warmup_frames += 1

                    if cfg.preview and warmup_frames % cfg.preview_every == 0:
                        component = buffer.payload.components[0]
                        raw, fmt = copy_raw_component(component)
                        preview = raw_to_bgr(raw, fmt)
                        max_w = 960
                        if preview.shape[1] > max_w:
                            scale = max_w / preview.shape[1]
                            preview = cv2.resize(
                                preview, None, fx=scale, fy=scale,
                                interpolation=cv2.INTER_AREA
                            )
                        cv2.imshow("BU238MCF Calibration Preview", preview)
                        if cv2.waitKey(1) & 0xFF == 27:
                            shutdown_event.set()
            except TimeoutException:
                print("[WARN] Camera fetch timeout during warm-up.")

        if cfg.white_balance_once:
            # Continue to allow the one-push operation to settle if requested.
            settle_end = local_clock() + cfg.white_balance_settle_seconds
            while not shutdown_event.is_set() and local_clock() < settle_end:
                try:
                    with ia.fetch(timeout=cfg.fetch_timeout_s):
                        pass
                except TimeoutException:
                    pass
            red, blue = finish_one_push_white_balance(ia)
            if red is not None:
                camera_info["one_push_balance_red"] = red
            if blue is not None:
                camera_info["one_push_balance_blue"] = blue

        print(
            f"[CAMERA] Warm-up complete: {warmup_frames} frames discarded. "
            "The camera remains free-running while waiting for PTB 900."
        )
        push_event(event_outlet, "CAMERA_WAITING_FOR_PTB_900")

        if not cfg.auto_ptb:
            input(
                "[CONTROL] Start LabRecorder, then press Enter to open "
                "the local recording gate..."
            )
            manual_now = float(local_clock())
            gate_open_lsl = manual_now
            push_event(event_outlet, "RECORDING_GATE_OPEN_MANUAL", manual_now)

        recording = not cfg.auto_ptb
        discard_after_start = cfg.discard_frames_after_start
        video_frame_index = 0
        last_recorded_frame_id: Optional[int] = None
        last_camera_s: Optional[float] = None
        last_host_s: Optional[float] = None
        dropped_total = 0
        stop_deadline_local: Optional[float] = None
        progress_start_lsl: Optional[float] = None

        while not shutdown_event.is_set():
            if writer.error is not None:
                raise RuntimeError(f"Video writer failed: {writer.error}")
            if cfg.auto_ptb and ptb is not None and ptb.error is not None:
                raise RuntimeError(f"PTB listener failed: {ptb.error}")

            if (
                cfg.auto_ptb
                and not recording
                and ptb is not None
                and ptb.record_start.is_set()
            ):
                gate_open_lsl = float(local_clock())
                recording = True
                discard_after_start = cfg.discard_frames_after_start
                progress_start_lsl = gate_open_lsl

                push_event(
                    event_outlet,
                    "PTB_900_RECEIVED_BY_CAMERA_APP",
                    ptb.start_received_camera_local,
                )
                push_event(
                    event_outlet,
                    "RECORDING_GATE_OPEN",
                    gate_open_lsl,
                )

                print()
                print("=" * 76)
                print("[RECORDING GATE]")
                print(
                    f"  PTB 900 received camera-local time : "
                    f"{ptb.start_received_camera_local:.9f} s"
                )
                print(
                    f"  Recording gate opened              : "
                    f"{gate_open_lsl:.9f} s"
                )
                print(
                    f"  Listener -> gate processing        : "
                    f"{(gate_open_lsl - ptb.start_received_camera_local) * 1000.0:.3f} ms"
                )
                print(
                    f"  Safety frames to discard           : "
                    f"{discard_after_start}"
                )
                print("=" * 76)

            if (
                cfg.auto_ptb
                and ptb is not None
                and ptb.record_stop.is_set()
                and stop_deadline_local is None
            ):
                assert ptb.stop_received_camera_local is not None
                stop_deadline_local = (
                    ptb.stop_received_camera_local + cfg.post_stop_seconds
                )
                push_event(
                    event_outlet,
                    "PTB_999_RECEIVED_BY_CAMERA_APP",
                    ptb.stop_received_camera_local,
                )
                print(
                    f"[STOP] Camera will record for another "
                    f"{cfg.post_stop_seconds:.3f} s; "
                    f"stop deadline={stop_deadline_local:.9f}."
                )

            if (
                stop_deadline_local is not None
                and local_clock() >= stop_deadline_local
            ):
                print(
                    "[STOP] Post-session buffer complete. "
                    "Stopping acquisition and finalizing the AVI."
                )
                break

            try:
                with ia.fetch(timeout=cfg.fetch_timeout_s) as buffer:
                    host_lsl_s = float(local_clock())
                    frame_id = require_camera_frame_id(buffer)
                    camera_s = require_camera_timestamp_s(buffer)

                    # While waiting for 900, keep fetching/discarding so the
                    # pipeline is warm and buffers cannot accumulate.
                    if not recording:
                        if cfg.preview:
                            component = buffer.payload.components[0]
                            raw, fmt = copy_raw_component(component)
                            if frame_id % cfg.preview_every == 0:
                                preview = raw_to_bgr(raw, fmt)
                                max_w = 960
                                if preview.shape[1] > max_w:
                                    scale = max_w / preview.shape[1]
                                    preview = cv2.resize(
                                        preview, None, fx=scale, fy=scale,
                                        interpolation=cv2.INTER_AREA
                                    )
                                cv2.imshow(
                                    "BU238MCF Calibration Preview", preview
                                )
                                if cv2.waitKey(1) & 0xFF == 27:
                                    shutdown_event.set()
                        continue

                    if discard_after_start > 0:
                        discard_after_start -= 1
                        print(
                            f"[RECORDING GATE] Discarded safety frame "
                            f"BlockID={frame_id}; remaining="
                            f"{discard_after_start}."
                        )
                        continue

                    dropped_this = 0
                    if last_recorded_frame_id is not None:
                        jump = frame_id - last_recorded_frame_id
                        if jump > 1:
                            dropped_this = int(jump - 1)
                            dropped_total += dropped_this
                            push_event(
                                event_outlet,
                                f"FRAME_DROP|missing={dropped_this}|"
                                f"after={last_recorded_frame_id}|now={frame_id}",
                                host_lsl_s,
                            )
                    last_recorded_frame_id = frame_id

                    camera_dt_ms = (
                        0.0 if last_camera_s is None
                        else (camera_s - last_camera_s) * 1000.0
                    )
                    host_dt_ms = (
                        0.0 if last_host_s is None
                        else (host_lsl_s - last_host_s) * 1000.0
                    )
                    last_camera_s = camera_s
                    last_host_s = host_lsl_s

                    component = buffer.payload.components[0]
                    raw, fmt = copy_raw_component(component)

                    video_frame_index += 1
                    packet = FramePacket(
                        video_frame_index=video_frame_index,
                        frame_id=frame_id,
                        raw=raw,
                        pixel_format=fmt,
                    )

                    try:
                        frame_queue.put(packet, timeout=0.5)
                    except queue.Full as exc:
                        push_event(
                            event_outlet,
                            "FATAL_VIDEO_WRITER_QUEUE_FULL",
                            host_lsl_s,
                        )
                        raise RuntimeError(
                            "The video writer fell more than the configured "
                            "queue behind. Recording stopped to protect "
                            "frame/video correspondence."
                        ) from exc

                    enqueue_lsl = float(local_clock())

                    # Four essential numeric values only. XDF time_stamps
                    # already contain host_lsl_s, so it is not duplicated.
                    metadata_outlet.push_sample(
                        [
                            float(frame_id),
                            float(video_frame_index),
                            float(camera_s),
                            float(dropped_total),
                        ],
                        timestamp=host_lsl_s,
                    )

                    csv_writer.writerow(
                        {
                            "frame_id": frame_id,
                            "video_frame_index": video_frame_index,
                            "camera_timestamp_s": f"{camera_s:.9f}",
                            "host_lsl_timestamp_s": f"{host_lsl_s:.9f}",
                            "camera_interframe_ms": f"{camera_dt_ms:.6f}",
                            "host_interframe_ms": f"{host_dt_ms:.6f}",
                            "dropped_this_frame": dropped_this,
                            "dropped_frame_total": dropped_total,
                        }
                    )

                    if first_frame_received_lsl is None:
                        first_frame_received_lsl = host_lsl_s
                        first_frame_enqueued_lsl = enqueue_lsl
                        first_frame_camera_s = camera_s
                        first_frame_id = frame_id

                        push_event(
                            event_outlet,
                            f"FIRST_RECORDED_FRAME_RECEIVED|frame_id={frame_id}",
                            host_lsl_s,
                        )

                        print()
                        print("=" * 76)
                        print("[FIRST RECORDED FRAME]")
                        print(
                            f"  Frame ID / BlockID                  : {frame_id}"
                        )
                        print(
                            f"  Camera timestamp                    : "
                            f"{camera_s:.9f} s (camera clock)"
                        )
                        print(
                            f"  Completed frame delivered to Python : "
                            f"{host_lsl_s:.9f} s (camera-PC LSL clock)"
                        )
                        print(
                            f"  Frame copied/enqueued               : "
                            f"{enqueue_lsl:.9f} s"
                        )
                        print(
                            f"  Receive -> enqueue                  : "
                            f"{(enqueue_lsl - host_lsl_s) * 1000.0:.3f} ms"
                        )
                        print("=" * 76)

                    if video_frame_index % cfg.progress_every_frames == 0:
                        csv_file.flush()
                        elapsed = (
                            host_lsl_s - progress_start_lsl
                            if progress_start_lsl is not None else 0.0
                        )
                        effective_fps = (
                            video_frame_index / elapsed
                            if elapsed > 0 else 0.0
                        )
                        q_depth = frame_queue.qsize()
                        print(
                            f"[RUN] video_frames={video_frame_index}, "
                            f"BlockID={frame_id}, "
                            f"effective_fps={effective_fps:.3f}, "
                            f"camera_drops={dropped_total}, "
                            f"writer_queue={q_depth}/{cfg.queue_size}"
                        )

                    if (
                        first_frame_received_lsl is not None
                        and writer.first_frame_written_lsl is not None
                        and not start_summary_printed
                    ):
                        push_event(
                            event_outlet,
                            "FIRST_RECORDED_FRAME_WRITTEN",
                            writer.first_frame_written_lsl,
                        )
                        print_start_delay_summary(
                            ptb,
                            gate_open_lsl,
                            first_frame_received_lsl,
                            first_frame_enqueued_lsl,
                            first_frame_camera_s,
                            first_frame_id,
                            writer,
                        )
                        start_summary_printed = True

                    if (
                        cfg.preview
                        and video_frame_index % cfg.preview_every == 0
                    ):
                        preview = raw_to_bgr(raw, fmt)
                        max_w = 960
                        if preview.shape[1] > max_w:
                            scale = max_w / preview.shape[1]
                            preview = cv2.resize(
                                preview, None, fx=scale, fy=scale,
                                interpolation=cv2.INTER_AREA
                            )
                        cv2.imshow("BU238MCF Recording Preview", preview)
                        if cv2.waitKey(1) & 0xFF == 27:
                            shutdown_event.set()

            except TimeoutException:
                push_event(event_outlet, "CAMERA_FETCH_TIMEOUT")
                print("[WARN] Camera fetch timeout.")

        stop_command_lsl = float(local_clock())
        print(f"[STOP] ia.stop() command issued: {stop_command_lsl:.9f} s")
        if ia.is_acquiring():
            ia.stop()
        stop_return_lsl = float(local_clock())
        print(f"[STOP] ia.stop() returned      : {stop_return_lsl:.9f} s")
        print(
            f"[STOP] ia.stop() call duration : "
            f"{(stop_return_lsl - stop_command_lsl) * 1000.0:.3f} ms"
        )

        writer_stop_event.set()
        writer.join(timeout=60)
        writer_finalized_lsl = float(local_clock())

        if writer.is_alive():
            raise RuntimeError(
                "Video writer did not finish within 60 seconds."
            )
        if writer.error is not None:
            raise RuntimeError(f"Video writer failed: {writer.error}")

        push_event(
            event_outlet,
            "CAMERA_RECORDING_STOPPED",
            writer_finalized_lsl,
        )

        print()
        print("=" * 76)
        print("[FINALIZATION]")
        print(f"  Frames received/enqueued : {video_frame_index}")
        print(f"  Frames written to AVI    : {writer.frames_written}")
        print(f"  Camera BlockID drops     : {dropped_total}")
        print(
            f"  Stop return -> AVI closed: "
            f"{(writer_finalized_lsl - stop_return_lsl) * 1000.0:.3f} ms"
        )
        print("=" * 76)

        summary["end_wall_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        summary["video_frames_received"] = video_frame_index
        summary["video_frames_written"] = writer.frames_written
        summary["camera_dropped_total"] = dropped_total
        summary["acquisition_start_command_lsl_s"] = (
            acquisition_start_command_lsl
        )
        summary["acquisition_start_return_lsl_s"] = (
            acquisition_start_return_lsl
        )
        summary["camera_stop_command_lsl_s"] = stop_command_lsl
        summary["camera_stop_return_lsl_s"] = stop_return_lsl
        summary["writer_finalized_lsl_s"] = writer_finalized_lsl

        if ptb is not None:
            start_timing: dict = {}
            add_if_value(
                start_timing, "ptb_900_remote_timestamp_s",
                ptb.start_remote_timestamp
            )
            add_if_value(
                start_timing, "ptb_time_correction_s",
                ptb.start_time_correction_s
            )
            add_if_value(
                start_timing, "ptb_900_camera_local_estimate_s",
                ptb.start_camera_local_estimate
            )
            add_if_value(
                start_timing, "ptb_900_received_camera_local_s",
                ptb.start_received_camera_local
            )
            add_if_value(
                start_timing, "recording_gate_open_lsl_s",
                gate_open_lsl
            )
            add_if_value(
                start_timing, "first_frame_received_lsl_s",
                first_frame_received_lsl
            )
            add_if_value(
                start_timing, "first_frame_enqueued_lsl_s",
                first_frame_enqueued_lsl
            )
            add_if_value(
                start_timing, "first_frame_written_lsl_s",
                writer.first_frame_written_lsl
            )
            add_if_value(
                start_timing, "first_frame_camera_timestamp_s",
                first_frame_camera_s
            )
            add_if_value(start_timing, "first_frame_id", first_frame_id)

            if (
                ptb.start_camera_local_estimate is not None
                and first_frame_received_lsl is not None
            ):
                start_timing["ptb_900_to_first_frame_delivery_ms"] = (
                    first_frame_received_lsl
                    - ptb.start_camera_local_estimate
                ) * 1000.0

            if (
                ptb.start_camera_local_estimate is not None
                and writer.first_frame_written_lsl is not None
            ):
                start_timing["ptb_900_to_first_frame_written_ms"] = (
                    writer.first_frame_written_lsl
                    - ptb.start_camera_local_estimate
                ) * 1000.0

            summary["start_timing"] = start_timing

            stop_timing: dict = {}
            add_if_value(
                stop_timing, "ptb_999_remote_timestamp_s",
                ptb.stop_remote_timestamp
            )
            add_if_value(
                stop_timing, "ptb_time_correction_s",
                ptb.stop_time_correction_s
            )
            add_if_value(
                stop_timing, "ptb_999_camera_local_estimate_s",
                ptb.stop_camera_local_estimate
            )
            add_if_value(
                stop_timing, "ptb_999_received_camera_local_s",
                ptb.stop_received_camera_local
            )
            summary["stop_timing"] = stop_timing

        return 0

    finally:
        shutdown_event.set()
        writer_stop_event.set()

        if ia is not None:
            try:
                if ia.is_acquiring():
                    ia.stop()
            except Exception:
                pass

        if writer is not None and writer.is_alive():
            writer.join(timeout=20)

        if csv_file is not None:
            try:
                csv_file.flush()
                csv_file.close()
            except Exception:
                pass

        if cfg.preview:
            cv2.destroyAllWindows()

        if ia is not None:
            try:
                ia.destroy()
            except Exception:
                pass

        try:
            h.reset()
        except Exception:
            pass

        try:
            summary_path.write_text(
                json.dumps(summary, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            print(f"[OUTPUT] Video  : {video_path}")
            print(f"[OUTPUT] Frames : {csv_path}")
            print(f"[OUTPUT] Summary: {summary_path}")
        except Exception as exc:
            print(f"[WARN] Could not save summary JSON: {exc}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        description=(
            "Record BU238MCF video at 60 fps and publish compact frame "
            "metadata through LSL."
        )
    )
    parser.add_argument("--cti", required=True, type=Path)
    parser.add_argument(
        "--output", type=Path, default=Path("Camera_Recordings")
    )
    parser.add_argument("--subject", default="S00")
    parser.add_argument("--serial", default=None)

    parser.add_argument("--fps", type=float, default=60.0)
    parser.add_argument("--exposure-us", type=float, default=8333.0)
    parser.add_argument("--gain-db", type=float, default=3.0)
    parser.add_argument("--gamma", type=float, default=1.0)
    parser.add_argument("--black-level", type=float, default=0.0)
    parser.add_argument("--pixel-format", default="BayerBG8")

    wb = parser.add_mutually_exclusive_group()
    wb.add_argument(
        "--white-balance-once",
        action="store_true",
        help="Run one-push white balance during camera warm-up.",
    )
    wb.add_argument(
        "--fixed-white-balance",
        action="store_true",
        help="Use --balance-red and --balance-blue.",
    )
    parser.add_argument("--balance-red", type=float, default=None)
    parser.add_argument("--balance-blue", type=float, default=None)
    parser.add_argument("--warmup-seconds", type=float, default=2.0)
    parser.add_argument(
        "--white-balance-settle-seconds", type=float, default=1.0
    )

    parser.add_argument("--queue-size", type=int, default=120)
    parser.add_argument("--camera-buffers", type=int, default=128)
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--preview-every", type=int, default=2)

    parser.add_argument("--manual", action="store_true")
    parser.add_argument("--ptb-stream", default="PTB_Triggers")
    parser.add_argument("--ptb-start-code", default="900")
    parser.add_argument("--ptb-stop-code", default="999")
    parser.add_argument(
        "--discard-frames-after-start", type=int, default=1
    )
    parser.add_argument("--post-stop-seconds", type=float, default=2.0)

    parser.add_argument("--fetch-timeout", type=float, default=1.0)
    parser.add_argument("--codec", default="MJPG")
    parser.add_argument(
        "--progress-every-frames",
        type=int,
        default=600,
        help="Print one progress line every N recorded frames.",
    )

    args = parser.parse_args()

    if len(args.codec) != 4:
        parser.error("--codec must contain exactly four characters.")

    if args.fixed_white_balance:
        if args.balance_red is None or args.balance_blue is None:
            parser.error(
                "--fixed-white-balance requires both --balance-red and "
                "--balance-blue."
            )
    elif args.balance_red is not None or args.balance_blue is not None:
        parser.error(
            "Use --fixed-white-balance when supplying manual balance ratios."
        )

    # If neither option was selected, do not change factory/current WB.
    white_balance_once = bool(args.white_balance_once)
    balance_red = args.balance_red if args.fixed_white_balance else None
    balance_blue = args.balance_blue if args.fixed_white_balance else None

    return Config(
        cti_path=args.cti.expanduser().resolve(),
        output_dir=args.output.expanduser().resolve(),
        subject=args.subject,
        serial=args.serial,

        fps=max(1.0, args.fps),
        exposure_us=max(30.0, args.exposure_us),
        gain_db=args.gain_db,
        gamma=args.gamma,
        black_level=args.black_level,
        pixel_format=args.pixel_format,

        white_balance_once=white_balance_once,
        balance_red=balance_red,
        balance_blue=balance_blue,
        warmup_seconds=max(0.0, args.warmup_seconds),
        white_balance_settle_seconds=max(
            0.0, args.white_balance_settle_seconds
        ),

        queue_size=max(10, args.queue_size),
        num_camera_buffers=max(8, args.camera_buffers),
        preview=args.preview,
        preview_every=max(1, args.preview_every),

        auto_ptb=not args.manual,
        ptb_stream_name=args.ptb_stream,
        ptb_start_code=str(args.ptb_start_code),
        ptb_stop_code=str(args.ptb_stop_code),
        discard_frames_after_start=max(
            0, args.discard_frames_after_start
        ),
        post_stop_seconds=max(0.0, args.post_stop_seconds),

        fetch_timeout_s=max(0.1, args.fetch_timeout),
        video_codec=args.codec,
        progress_every_frames=max(1, args.progress_every_frames),
    )


if __name__ == "__main__":
    try:
        sys.exit(run(parse_args()))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print(f"\n[FATAL] {exc}", file=sys.stderr)
        sys.exit(1)
