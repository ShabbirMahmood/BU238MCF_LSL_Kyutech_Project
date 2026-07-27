#!/usr/bin/env python3
"""
bu238mcf_lsl_30fps.py

Minimal 30 FPS local video recorder + LSL frame-metadata outlet for the
Toshiba Teli BU238MCF camera.

Two run modes:
    manual : press Enter to start, press Enter again to stop
    ptb    : PTB_Triggers code 900 starts recording; code 999 stops recording

The camera is free-running and warmed before recording begins. This avoids
including camera-start initialization in the experimental start delay.

LSL streams:
    Camera_Frame_Metadata
        channel 1: frame_id           (camera/GenTL BlockID)
        channel 2: video_frame_index  (1-based index in the local AVI)
        channel 3: dropped_total      (missing BlockIDs during recording)

        The XDF timestamp of each sample is the camera-PC LSL time measured
        immediately after a completed frame buffer reaches Python.

    Camera_Events
        Essential camera status, start/stop, first-frame, and anomaly markers.

Local outputs:
    *_camera_30fps.avi
    *_camera_frames.csv
    *_camera_summary.json

Timing limitation:
    Frame metadata timestamps represent completed-frame delivery to Python,
    not physical exposure onset. Use the camera ExposureActive GPIO recorded
    by an isolated EEG AUX/digital input for physical exposure timing.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import queue
import signal
import sys
import threading
import time
from dataclasses import dataclass
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

# Fixed acquisition format requested for this project.
FIXED_FPS = 30.0
FIXED_WIDTH = 1280      # 1920
FIXED_HEIGHT = 800         # 1200


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    cti_path: Path
    output_dir: Path
    serial: Optional[str]

    exposure_us: float
    gain_db: float
    gamma: float
    black_level: float
    balance_red: float
    balance_blue: float
    pixel_format: str

    warmup_seconds: float
    post_stop_seconds: float
    discard_frames_after_start: int
    camera_buffers: int
    writer_queue_frames: int
    preview_every: int
    progress_every_frames: int
    fetch_timeout_seconds: float
    video_codec: str

    ptb_stream_name: str
    ptb_start_code: str
    ptb_stop_code: str


def load_settings(path: Path) -> Settings:
    if not path.is_file():
        raise FileNotFoundError(f"Settings file was not found:\n{path}")

    raw = json.loads(path.read_text(encoding="utf-8"))

    required = [
        "cti_path",
        "output_dir",
        "exposure_us",
        "gain_db",
        "gamma",
        "black_level",
        "balance_red",
        "balance_blue",
        "pixel_format",
    ]
    missing = [name for name in required if name not in raw]
    if missing:
        raise ValueError(
            "Missing required settings: " + ", ".join(missing)
        )

    cti_path = Path(raw["cti_path"]).expanduser()
    output_dir = Path(raw["output_dir"]).expanduser()

    # Relative paths are resolved from the settings file directory.
    if not cti_path.is_absolute():
        cti_path = (path.parent / cti_path).resolve()
    else:
        cti_path = cti_path.resolve()

    if not output_dir.is_absolute():
        output_dir = (path.parent / output_dir).resolve()
    else:
        output_dir = output_dir.resolve()

    codec = str(raw.get("video_codec", "MJPG"))
    if len(codec) != 4:
        raise ValueError("video_codec must contain exactly four characters.")

    return Settings(
        cti_path=cti_path,
        output_dir=output_dir,
        serial=(str(raw.get("serial", "")).strip() or None),

        exposure_us=float(raw["exposure_us"]),
        gain_db=float(raw["gain_db"]),
        gamma=float(raw["gamma"]),
        black_level=float(raw["black_level"]),
        balance_red=float(raw["balance_red"]),
        balance_blue=float(raw["balance_blue"]),
        pixel_format=str(raw["pixel_format"]),

        warmup_seconds=max(0.0, float(raw.get("warmup_seconds", 2.0))),
        post_stop_seconds=max(
            0.0, float(raw.get("post_stop_seconds", 2.0))
        ),
        discard_frames_after_start=max(
            0, int(raw.get("discard_frames_after_start", 1))
        ),
        camera_buffers=max(8, int(raw.get("camera_buffers", 128))),
        writer_queue_frames=max(
            10, int(raw.get("writer_queue_frames", 120))
        ),
        preview_every=max(1, int(raw.get("preview_every", 2))),
        progress_every_frames=max(
            1, int(raw.get("progress_every_frames", 300))
        ),
        fetch_timeout_seconds=max(
            0.1, float(raw.get("fetch_timeout_seconds", 1.0))
        ),
        video_codec=codec,

        ptb_stream_name=str(raw.get("ptb_stream_name", "PTB_Triggers")),
        ptb_start_code=str(raw.get("ptb_start_code", "900")),
        ptb_stop_code=str(raw.get("ptb_stop_code", "999")),
    )


# ---------------------------------------------------------------------------
# Camera-node helpers
# ---------------------------------------------------------------------------

def set_enum(node_map, name: str, value: str, required: bool = True) -> bool:
    node = getattr(node_map, name, None)
    if node is None:
        if required:
            raise RuntimeError(f"Required camera node is missing: {name}")
        return False
    try:
        node.value = value
        return True
    except Exception as exc:
        if required:
            raise RuntimeError(f"Could not set {name}={value}: {exc}") from exc
        return False


def set_bool(node_map, name: str, value: bool) -> bool:
    node = getattr(node_map, name, None)
    if node is None:
        return False
    try:
        node.value = bool(value)
        return True
    except Exception:
        return False


def set_float(
    node_map,
    name: str,
    value: float,
    required: bool = True,
) -> Optional[float]:
    node = getattr(node_map, name, None)
    if node is None:
        if required:
            raise RuntimeError(f"Required camera node is missing: {name}")
        return None
    try:
        target = float(value)
        try:
            target = max(float(node.min), min(float(node.max), target))
        except Exception:
            pass
        node.value = target
        return float(node.value)
    except Exception as exc:
        if required:
            raise RuntimeError(f"Could not set {name}={value}: {exc}") from exc
        return None


def set_int(
    node_map,
    name: str,
    value: int,
    required: bool = True,
) -> Optional[int]:
    node = getattr(node_map, name, None)
    if node is None:
        if required:
            raise RuntimeError(f"Required camera node is missing: {name}")
        return None
    try:
        target = int(value)
        try:
            target = max(int(node.min), min(int(node.max), target))
        except Exception:
            pass
        node.value = target
        return int(node.value)
    except Exception as exc:
        if required:
            raise RuntimeError(f"Could not set {name}={value}: {exc}") from exc
        return None


def get_value(node_map, name: str, default=None):
    node = getattr(node_map, name, None)
    if node is None:
        return default
    try:
        return node.value
    except Exception:
        return default


def set_balance_ratio(node_map, selector: str, value: float) -> float:
    set_enum(node_map, "BalanceWhiteAuto", "Off", required=False)
    set_enum(node_map, "BalanceRatioSelector", selector, required=True)
    actual = set_float(node_map, "BalanceRatio", value, required=True)
    assert actual is not None
    return actual


def configure_camera(ia, settings: Settings) -> dict:
    nm = ia.remote_device.node_map

    set_enum(nm, "AcquisitionMode", "Continuous")
    set_enum(nm, "TriggerMode", "Off")

    # Fix the full sensor area.
    # set_int(nm, "OffsetX", 0, required=False)
    # set_int(nm, "OffsetY", 0, required=False)
    
    set_int(nm, "OffsetX", 320, required=False)
    set_int(nm, "OffsetY", 200, required=False)

    actual_width = set_int(nm, "Width", FIXED_WIDTH)
    actual_height = set_int(nm, "Height", FIXED_HEIGHT)

    set_enum(nm, "PixelFormat", settings.pixel_format)

    set_bool(nm, "AcquisitionFrameRateEnable", True)
    actual_fps = set_float(
        nm, "AcquisitionFrameRate", FIXED_FPS, required=True
    )

    # Force fixed/manual image parameters.
    set_enum(nm, "ExposureAuto", "Off", required=False)
    set_enum(nm, "ExposureTimeControl", "Manual", required=False)
    actual_exposure = set_float(
        nm, "ExposureTime", settings.exposure_us
    )

    set_enum(nm, "GainAuto", "Off", required=False)
    actual_gain = set_float(nm, "Gain", settings.gain_db)
    actual_gamma = set_float(nm, "Gamma", settings.gamma)
    actual_black = set_float(
        nm, "BlackLevel", settings.black_level
    )

    set_enum(nm, "BalanceWhiteAuto", "Off", required=False)
    actual_red = set_balance_ratio(
        nm, "Red", settings.balance_red
    )
    actual_blue = set_balance_ratio(
        nm, "Blue", settings.balance_blue
    )

    try:
        ia.num_buffers = max(
            settings.camera_buffers, int(ia.min_num_buffers)
        )
    except Exception as exc:
        print(f"[WARN] Could not set camera buffers: {exc}")

    model = str(get_value(nm, "DeviceModelName", "unknown"))
    serial = str(get_value(nm, "DeviceSerialNumber", "unknown"))

    actual = {
        "model": model,
        "serial": serial,
        "width": actual_width,
        "height": actual_height,
        "fps": actual_fps,
        "pixel_format": settings.pixel_format,
        "exposure_us": actual_exposure,
        "gain_db": actual_gain,
        "gamma": actual_gamma,
        "black_level": actual_black,
        "balance_red": actual_red,
        "balance_blue": actual_blue,
        "camera_buffers": getattr(ia, "num_buffers", None),
    }

    print()
    print("[CAMERA SETTINGS]")
    print(
        f"  {actual_width} x {actual_height} @ "
        f"{actual_fps:.3f} FPS, {settings.pixel_format}"
    )
    print(
        f"  Exposure={actual_exposure:.1f} us | "
        f"Gain={actual_gain:.2f} dB | "
        f"Gamma={actual_gamma:.2f} | "
        f"BlackLevel={actual_black:.2f}"
    )
    print(
        f"  White balance: Red={actual_red:.6f}, "
        f"Blue={actual_blue:.6f}"
    )
    print()

    return actual


# ---------------------------------------------------------------------------
# Frame conversion
# ---------------------------------------------------------------------------

def camera_frame_id(buffer) -> int:
    try:
        return int(buffer.frame_id)
    except Exception as exc:
        raise RuntimeError(
            "The GenTL producer did not expose buffer.frame_id/BlockID."
        ) from exc


def copy_raw_component(component) -> tuple[np.ndarray, str]:
    width = int(component.width)
    height = int(component.height)
    fmt = str(component.data_format)
    data = component.data

    if data is None:
        raise RuntimeError("Camera returned an empty image component.")

    arr = np.asarray(data)
    expected = width * height
    if arr.size != expected:
        raise RuntimeError(
            f"Unexpected image size for {fmt}: {arr.size}, "
            f"expected {expected}."
        )

    return arr.reshape(height, width).astype(np.uint8, copy=True), fmt


def raw_to_bgr(raw: np.ndarray, fmt: str) -> np.ndarray:
    conversions = {
        "Mono8": cv2.COLOR_GRAY2BGR,
        "BayerBG8": cv2.COLOR_BAYER_RG2BGR,
        "BayerGB8": cv2.COLOR_BAYER_GR2BGR,
        "BayerGR8": cv2.COLOR_BAYER_GB2BGR,
        "BayerRG8": cv2.COLOR_BAYER_BG2BGR,
    }
    if fmt not in conversions:
        raise RuntimeError(
            f"Unsupported pixel format {fmt!r}. "
            "Set pixel_format to the exact Bayer format shown by TeliViewer."
        )
    return cv2.cvtColor(raw, conversions[fmt])


# ---------------------------------------------------------------------------
# LSL
# ---------------------------------------------------------------------------

def create_lsl_outlets(source_id: str):
    metadata_info = StreamInfo(
        "Camera_Frame_Metadata",
        "VideoFrameMetadata",
        3,
        FIXED_FPS,
        "double64",
        source_id,
    )

    channels = metadata_info.desc().append_child("channels")
    for label, unit in [
        ("frame_id", "count"),
        ("video_frame_index", "count"),
        ("dropped_frame_total", "count"),
    ]:
        channel = channels.append_child("channel")
        channel.append_child_value("label", label)
        channel.append_child_value("unit", unit)
        channel.append_child_value("type", "CameraMetadata")

    metadata_info.desc().append_child_value(
        "timestamp_meaning",
        "XDF sample timestamps are camera-PC LSL times taken immediately "
        "after completed-frame delivery to Python.",
    )

    event_info = StreamInfo(
        "Camera_Events",
        "Markers",
        1,
        0.0,
        "string",
        source_id + "_events",
    )

    return (
        StreamOutlet(metadata_info, chunk_size=1, max_buffered=360),
        StreamOutlet(event_info),
    )


def send_event(
    outlet: StreamOutlet,
    message: str,
    timestamp: Optional[float] = None,
    print_event: bool = True,
):
    ts = float(local_clock()) if timestamp is None else float(timestamp)
    outlet.push_sample([message], timestamp=ts)
    if print_event:
        print(f"[EVENT] {message}")


def normalize_code(value) -> str:
    text = str(value).strip()
    try:
        number = float(text)
        if math.isfinite(number) and number.is_integer():
            return str(int(number))
    except (TypeError, ValueError):
        pass
    return text


class PTBController(threading.Thread):
    def __init__(self, settings: Settings, shutdown: threading.Event):
        super().__init__(daemon=True)
        self.settings = settings
        self.shutdown = shutdown
        self.start_event = threading.Event()
        self.stop_event = threading.Event()
        self.error: Optional[BaseException] = None

        self.correction_s: Optional[float] = None

        self.start_remote_s: Optional[float] = None
        self.start_mapped_local_s: Optional[float] = None
        self.start_received_local_s: Optional[float] = None

        self.stop_remote_s: Optional[float] = None
        self.stop_mapped_local_s: Optional[float] = None
        self.stop_received_local_s: Optional[float] = None

    @staticmethod
    def get_time_correction(inlet: StreamInlet) -> float:
        try:
            return float(inlet.time_correction(timeout=5.0))
        except TypeError:
            return float(inlet.time_correction(5.0))

    def run(self):
        try:
            print(
                f"[PTB] Waiting for {self.settings.ptb_stream_name!r} ..."
            )
            streams = []
            while not self.shutdown.is_set() and not streams:
                streams = resolve_byprop(
                    "name",
                    self.settings.ptb_stream_name,
                    minimum=1,
                    timeout=1.0,
                )
            if not streams:
                return

            inlet = StreamInlet(streams[0], max_buflen=60, recover=True)
            self.correction_s = self.get_time_correction(inlet)
            print("[PTB] Connected. Waiting for trigger 900.")

            while not self.shutdown.is_set():
                sample, remote_timestamp = inlet.pull_sample(timeout=0.2)
                if sample is None:
                    continue

                code = normalize_code(sample[0])
                if code not in {
                    self.settings.ptb_start_code,
                    self.settings.ptb_stop_code,
                }:
                    continue

                local_receive = float(local_clock())
                correction = float(self.correction_s or 0.0)
                mapped_local = float(remote_timestamp) + correction

                if (
                    code == self.settings.ptb_start_code
                    and not self.start_event.is_set()
                ):
                    self.start_remote_s = float(remote_timestamp)
                    self.start_mapped_local_s = mapped_local
                    self.start_received_local_s = local_receive
                    self.start_event.set()

                    delivery_ms = (
                        local_receive - mapped_local
                    ) * 1000.0
                    print(
                        f"[START] Trigger 900 received | "
                        f"LSL delivery/scheduling={delivery_ms:.3f} ms"
                    )

                elif (
                    code == self.settings.ptb_stop_code
                    and not self.stop_event.is_set()
                ):
                    self.stop_remote_s = float(remote_timestamp)
                    self.stop_mapped_local_s = mapped_local
                    self.stop_received_local_s = local_receive
                    self.stop_event.set()

                    delivery_ms = (
                        local_receive - mapped_local
                    ) * 1000.0
                    print(
                        f"[STOP] Trigger 999 received | "
                        f"LSL delivery/scheduling={delivery_ms:.3f} ms"
                    )

        except BaseException as exc:
            self.error = exc
            self.shutdown.set()


class ManualController(threading.Thread):
    def __init__(self, shutdown: threading.Event):
        super().__init__(daemon=True)
        self.shutdown = shutdown
        self.start_event = threading.Event()
        self.stop_event = threading.Event()
        self.start_local_s: Optional[float] = None
        self.stop_local_s: Optional[float] = None

    def run(self):
        try:
            input(
                "[MANUAL] Start LabRecorder, then press Enter to START "
                "camera recording..."
            )
            self.start_local_s = float(local_clock())
            self.start_event.set()

            input("[MANUAL] Press Enter again to STOP recording...")
            self.stop_local_s = float(local_clock())
            self.stop_event.set()
        except (EOFError, KeyboardInterrupt):
            self.shutdown.set()


# ---------------------------------------------------------------------------
# Background writer
# ---------------------------------------------------------------------------

@dataclass
class FramePacket:
    video_index: int
    raw: np.ndarray
    pixel_format: str


class VideoWriterWorker(threading.Thread):
    def __init__(
        self,
        video_path: Path,
        codec: str,
        frame_queue: queue.Queue,
        stop_event: threading.Event,
    ):
        super().__init__(daemon=True)
        self.video_path = video_path
        self.codec = codec
        self.frame_queue = frame_queue
        self.stop_event = stop_event

        self.writer: Optional[cv2.VideoWriter] = None
        self.frames_written = 0
        self.first_written_lsl_s: Optional[float] = None
        self.error: Optional[BaseException] = None
        self.max_queue_depth = 0

    def open_writer(self, frame: np.ndarray):
        height, width = frame.shape[:2]
        fourcc = cv2.VideoWriter_fourcc(*self.codec)
        self.writer = cv2.VideoWriter(
            str(self.video_path),
            fourcc,
            FIXED_FPS,
            (width, height),
            True,
        )
        if not self.writer.isOpened():
            raise RuntimeError(
                f"Could not open video writer: {self.video_path}"
            )

    def run(self):
        try:
            while not self.stop_event.is_set() or not self.frame_queue.empty():
                try:
                    packet: FramePacket = self.frame_queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                self.max_queue_depth = max(
                    self.max_queue_depth, self.frame_queue.qsize()
                )

                frame = raw_to_bgr(packet.raw, packet.pixel_format)
                if self.writer is None:
                    self.open_writer(frame)

                self.writer.write(frame)
                self.frames_written += 1

                if self.first_written_lsl_s is None:
                    self.first_written_lsl_s = float(local_clock())

                self.frame_queue.task_done()

        except BaseException as exc:
            self.error = exc
        finally:
            if self.writer is not None:
                self.writer.release()


# ---------------------------------------------------------------------------
# Acquisition
# ---------------------------------------------------------------------------

def run(
    settings: Settings,
    subject: str,
    mode: str,
    preview: bool,
) -> int:
    if not settings.cti_path.is_file():
        raise FileNotFoundError(
            f"Toshiba GenTL CTI file was not found:\n{settings.cti_path}"
        )

    settings.output_dir.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    prefix = f"{subject}_{stamp}_{mode}"
    video_path = settings.output_dir / f"{prefix}_camera_30fps.avi"
    csv_path = settings.output_dir / f"{prefix}_camera_frames.csv"
    json_path = settings.output_dir / f"{prefix}_camera_summary.json"

    shutdown = threading.Event()
    writer_stop = threading.Event()
    frame_queue: queue.Queue = queue.Queue(
        maxsize=settings.writer_queue_frames
    )

    def request_shutdown(signum=None, frame=None):
        del signum, frame
        shutdown.set()

    signal.signal(signal.SIGINT, request_shutdown)
    try:
        signal.signal(signal.SIGTERM, request_shutdown)
    except Exception:
        pass

    h = Harvester()
    ia = None
    writer: Optional[VideoWriterWorker] = None
    csv_file = None
    metadata_outlet = None
    event_outlet = None
    controller = None

    summary = {
        "subject": subject,
        "mode": mode,
        "start_datetime": time.strftime("%Y-%m-%d %H:%M:%S"),
        "video_file": str(video_path),
        "frame_csv": str(csv_path),
        "frame_rate_fixed_fps": FIXED_FPS,
        "resolution_fixed": [FIXED_WIDTH, FIXED_HEIGHT],
        "timestamp_meaning": (
            "Camera_Frame_Metadata XDF timestamps are camera-PC LSL times "
            "at completed-frame delivery to Python."
        ),
    }

    # Timing values.
    gate_open_s: Optional[float] = None
    first_frame_receive_s: Optional[float] = None
    first_frame_enqueue_s: Optional[float] = None
    first_frame_id_value: Optional[int] = None
    start_summary_printed = False
    stop_deadline_s: Optional[float] = None

    try:
        # Reduce Python-side logging; some GenTL producer messages may still
        # be printed by the vendor library itself.
        logging.getLogger().setLevel(logging.WARNING)

        print("[INIT] Loading Toshiba GenTL producer.")
        h.add_file(str(settings.cti_path))
        h.update()

        if not h.device_info_list:
            raise RuntimeError(
                "No camera detected. Verify it in TeliViewer, then close "
                "TeliViewer before running this program."
            )

        ia = (
            h.create({"serial_number": settings.serial})
            if settings.serial else h.create(0)
        )

        actual_camera_settings = configure_camera(ia, settings)
        summary["camera_settings"] = actual_camera_settings

        source_id = (
            f"ToshibaTeli_{actual_camera_settings['model']}_"
            f"{actual_camera_settings['serial']}"
        )
        metadata_outlet, event_outlet = create_lsl_outlets(source_id)
        send_event(event_outlet, "CAMERA_STREAM_READY")
        print(
            "[READY] LSL streams: Camera_Frame_Metadata, Camera_Events"
        )

        writer = VideoWriterWorker(
            video_path,
            settings.video_codec,
            frame_queue,
            writer_stop,
        )
        writer.start()

        csv_file = csv_path.open("w", newline="", encoding="utf-8")
        csv_writer = csv.DictWriter(
            csv_file,
            fieldnames=[
                "frame_id",
                "video_frame_index",
                "host_lsl_timestamp_s",
                "host_interframe_ms",
                "dropped_this_frame",
                "dropped_frame_total",
            ],
        )
        csv_writer.writeheader()

        if mode == "ptb":
            controller = PTBController(settings, shutdown)
        else:
            controller = ManualController(shutdown)
        controller.start()

        acquisition_command_s = float(local_clock())
        ia.start()
        acquisition_return_s = float(local_clock())
        send_event(
            event_outlet,
            "CAMERA_ACQUISITION_RUNNING",
            acquisition_return_s,
        )
        print(
            f"[CAMERA] Acquisition started in "
            f"{(acquisition_return_s - acquisition_command_s) * 1000.0:.3f} ms"
        )

        # Warm-up while continuously draining the camera buffers.
        warmup_end = local_clock() + settings.warmup_seconds
        warmup_frames = 0
        while not shutdown.is_set() and local_clock() < warmup_end:
            try:
                with ia.fetch(
                    timeout=settings.fetch_timeout_seconds
                ) as buffer:
                    warmup_frames += 1
                    if preview and warmup_frames % settings.preview_every == 0:
                        raw, fmt = copy_raw_component(
                            buffer.payload.components[0]
                        )
                        image = raw_to_bgr(raw, fmt)
                        display = image
                        if display.shape[1] > 960:
                            scale = 960 / display.shape[1]
                            display = cv2.resize(
                                display,
                                None,
                                fx=scale,
                                fy=scale,
                                interpolation=cv2.INTER_AREA,
                            )
                        cv2.imshow("BU238MCF 30 FPS Preview", display)
                        if cv2.waitKey(1) & 0xFF == 27:
                            shutdown.set()
            except TimeoutException:
                print("[WARN] Camera timeout during warm-up.")

        print(
            f"[READY] Warm-up complete ({warmup_frames} frames discarded)."
        )
        if mode == "ptb":
            print("[READY] Start LabRecorder; camera is waiting for PTB 900.")

        recording = False
        discard_remaining = settings.discard_frames_after_start

        video_frame_index = 0
        last_frame_id: Optional[int] = None
        last_host_s: Optional[float] = None
        dropped_total = 0
        progress_origin_s: Optional[float] = None

        while not shutdown.is_set():
            if writer.error is not None:
                raise RuntimeError(f"Video writer failed: {writer.error}")
            if (
                isinstance(controller, PTBController)
                and controller.error is not None
            ):
                raise RuntimeError(
                    f"PTB listener failed: {controller.error}"
                )

            if not recording and controller.start_event.is_set():
                recording = True
                gate_open_s = float(local_clock())
                progress_origin_s = gate_open_s
                discard_remaining = settings.discard_frames_after_start
                send_event(
                    event_outlet,
                    "RECORDING_STARTED",
                    gate_open_s,
                )
                print("[RECORDING] Started.")

            if (
                controller.stop_event.is_set()
                and stop_deadline_s is None
            ):
                stop_reference = (
                    controller.stop_received_local_s
                    if isinstance(controller, PTBController)
                    else controller.stop_local_s
                )
                if stop_reference is None:
                    stop_reference = float(local_clock())

                stop_deadline_s = (
                    float(stop_reference) + settings.post_stop_seconds
                )
                send_event(
                    event_outlet,
                    "STOP_REQUEST_RECEIVED",
                    float(stop_reference),
                )
                print(
                    f"[RECORDING] Stop requested; "
                    f"{settings.post_stop_seconds:.1f} s post-buffer."
                )

            if (
                stop_deadline_s is not None
                and local_clock() >= stop_deadline_s
            ):
                break

            try:
                with ia.fetch(
                    timeout=settings.fetch_timeout_seconds
                ) as buffer:
                    host_lsl_s = float(local_clock())
                    frame_id = camera_frame_id(buffer)

                    if not recording:
                        if preview and frame_id % settings.preview_every == 0:
                            raw, fmt = copy_raw_component(
                                buffer.payload.components[0]
                            )
                            image = raw_to_bgr(raw, fmt)
                            display = image
                            if display.shape[1] > 960:
                                scale = 960 / display.shape[1]
                                display = cv2.resize(
                                    display,
                                    None,
                                    fx=scale,
                                    fy=scale,
                                    interpolation=cv2.INTER_AREA,
                                )
                            cv2.imshow("BU238MCF 30 FPS Preview", display)
                            if cv2.waitKey(1) & 0xFF == 27:
                                shutdown.set()
                        continue

                    if discard_remaining > 0:
                        discard_remaining -= 1
                        continue

                    dropped_this = 0
                    if last_frame_id is not None:
                        jump = frame_id - last_frame_id
                        if jump > 1:
                            dropped_this = int(jump - 1)
                            dropped_total += dropped_this
                            send_event(
                                event_outlet,
                                f"FRAME_DROP|missing={dropped_this}|"
                                f"after={last_frame_id}|now={frame_id}",
                                host_lsl_s,
                            )
                            print(
                                f"[WARN] Camera frame loss: "
                                f"{dropped_this} frame(s)."
                            )
                    last_frame_id = frame_id

                    host_dt_ms = (
                        0.0
                        if last_host_s is None
                        else (host_lsl_s - last_host_s) * 1000.0
                    )
                    last_host_s = host_lsl_s

                    raw, fmt = copy_raw_component(
                        buffer.payload.components[0]
                    )

                    video_frame_index += 1
                    packet = FramePacket(
                        video_index=video_frame_index,
                        raw=raw,
                        pixel_format=fmt,
                    )

                    try:
                        frame_queue.put(packet, timeout=1.0)
                    except queue.Full as exc:
                        send_event(
                            event_outlet,
                            "FATAL_VIDEO_WRITER_QUEUE_FULL",
                            host_lsl_s,
                        )
                        raise RuntimeError(
                            "Video writer queue is full. Use a local SSD, "
                            "disable other heavy programs, or reduce image "
                            "processing load."
                        ) from exc

                    enqueue_s = float(local_clock())

                    metadata_outlet.push_sample(
                        [
                            float(frame_id),
                            float(video_frame_index),
                            float(dropped_total),
                        ],
                        timestamp=host_lsl_s,
                    )

                    csv_writer.writerow(
                        {
                            "frame_id": frame_id,
                            "video_frame_index": video_frame_index,
                            "host_lsl_timestamp_s": f"{host_lsl_s:.9f}",
                            "host_interframe_ms": f"{host_dt_ms:.6f}",
                            "dropped_this_frame": dropped_this,
                            "dropped_frame_total": dropped_total,
                        }
                    )

                    if first_frame_receive_s is None:
                        first_frame_receive_s = host_lsl_s
                        first_frame_enqueue_s = enqueue_s
                        first_frame_id_value = frame_id
                        send_event(
                            event_outlet,
                            f"FIRST_FRAME|frame_id={frame_id}",
                            host_lsl_s,
                        )

                    if (
                        first_frame_receive_s is not None
                        and writer.first_written_lsl_s is not None
                        and not start_summary_printed
                    ):
                        if isinstance(controller, PTBController):
                            reference_s = (
                                controller.start_mapped_local_s
                                if controller.start_mapped_local_s is not None
                                else gate_open_s
                            )
                            label = "PTB 900"
                        else:
                            reference_s = controller.start_local_s
                            label = "Manual start"

                        if reference_s is not None:
                            print(
                                f"[START DELAY] {label} -> gate: "
                                f"{(gate_open_s - reference_s) * 1000.0:.3f} ms"
                            )
                            print(
                                f"[START DELAY] {label} -> first frame: "
                                f"{(first_frame_receive_s - reference_s) * 1000.0:.3f} ms"
                            )
                            print(
                                f"[START DELAY] {label} -> first AVI write: "
                                f"{(writer.first_written_lsl_s - reference_s) * 1000.0:.3f} ms"
                            )
                        start_summary_printed = True

                    if (
                        video_frame_index
                        % settings.progress_every_frames
                        == 0
                    ):
                        csv_file.flush()
                        elapsed = (
                            host_lsl_s - progress_origin_s
                            if progress_origin_s is not None
                            else 0.0
                        )
                        effective_fps = (
                            video_frame_index / elapsed
                            if elapsed > 0 else 0.0
                        )
                        print(
                            f"[RUN] frames={video_frame_index} | "
                            f"FPS={effective_fps:.2f} | "
                            f"drops={dropped_total} | "
                            f"queue={frame_queue.qsize()}/"
                            f"{settings.writer_queue_frames}"
                        )

                    if (
                        preview
                        and video_frame_index
                        % settings.preview_every
                        == 0
                    ):
                        image = raw_to_bgr(raw, fmt)
                        display = image
                        if display.shape[1] > 960:
                            scale = 960 / display.shape[1]
                            display = cv2.resize(
                                display,
                                None,
                                fx=scale,
                                fy=scale,
                                interpolation=cv2.INTER_AREA,
                            )
                        cv2.imshow("BU238MCF 30 FPS Preview", display)
                        if cv2.waitKey(1) & 0xFF == 27:
                            shutdown.set()

            except TimeoutException:
                print("[WARN] Camera fetch timeout.")
                send_event(
                    event_outlet,
                    "CAMERA_FETCH_TIMEOUT",
                    print_event=False,
                )

        # Stop and finalize.
        camera_stop_command_s = float(local_clock())
        if ia.is_acquiring():
            ia.stop()
        camera_stop_return_s = float(local_clock())

        writer_stop.set()
        writer.join(timeout=60)
        writer_closed_s = float(local_clock())

        if writer.is_alive():
            raise RuntimeError("Video writer did not finish within 60 s.")
        if writer.error is not None:
            raise RuntimeError(f"Video writer failed: {writer.error}")

        send_event(
            event_outlet,
            "CAMERA_RECORDING_STOPPED",
            writer_closed_s,
        )

        if isinstance(controller, PTBController):
            stop_reference_s = controller.stop_mapped_local_s
            stop_label = "PTB 999"
        else:
            stop_reference_s = controller.stop_local_s
            stop_label = "Manual stop"

        if stop_reference_s is not None:
            print(
                f"[STOP DELAY] {stop_label} -> camera stop command: "
                f"{(camera_stop_command_s - stop_reference_s) * 1000.0:.3f} ms"
            )
            print(
                f"[STOP DELAY] {stop_label} -> AVI closed: "
                f"{(writer_closed_s - stop_reference_s) * 1000.0:.3f} ms"
            )

        print()
        print("[COMPLETE]")
        print(f"  Video frames written : {writer.frames_written}")
        print(f"  Camera frame drops   : {dropped_total}")
        print(f"  Max writer queue     : {writer.max_queue_depth}")
        print(f"  Video                : {video_path}")
        print(f"  Frame log            : {csv_path}")

        summary.update(
            {
                "end_datetime": time.strftime("%Y-%m-%d %H:%M:%S"),
                "video_frames_received": video_frame_index,
                "video_frames_written": writer.frames_written,
                "camera_dropped_total": dropped_total,
                "writer_max_queue_depth": writer.max_queue_depth,
                "camera_start_command_lsl_s": acquisition_command_s,
                "camera_start_return_lsl_s": acquisition_return_s,
                "recording_gate_open_lsl_s": gate_open_s,
                "first_frame_receive_lsl_s": first_frame_receive_s,
                "first_frame_enqueue_lsl_s": first_frame_enqueue_s,
                "first_frame_written_lsl_s": writer.first_written_lsl_s,
                "first_frame_id": first_frame_id_value,
                "camera_stop_command_lsl_s": camera_stop_command_s,
                "camera_stop_return_lsl_s": camera_stop_return_s,
                "writer_closed_lsl_s": writer_closed_s,
            }
        )

        if isinstance(controller, PTBController):
            summary["ptb_timing"] = {
                "start_remote_timestamp_s": controller.start_remote_s,
                "start_mapped_camera_local_s":
                    controller.start_mapped_local_s,
                "start_received_camera_local_s":
                    controller.start_received_local_s,
                "stop_remote_timestamp_s": controller.stop_remote_s,
                "stop_mapped_camera_local_s":
                    controller.stop_mapped_local_s,
                "stop_received_camera_local_s":
                    controller.stop_received_local_s,
                "cached_time_correction_s": controller.correction_s,
            }

            if (
                controller.start_mapped_local_s is not None
                and first_frame_receive_s is not None
            ):
                summary[
                    "ptb_900_to_first_frame_delivery_ms"
                ] = (
                    first_frame_receive_s
                    - controller.start_mapped_local_s
                ) * 1000.0

            if (
                controller.start_mapped_local_s is not None
                and writer.first_written_lsl_s is not None
            ):
                summary[
                    "ptb_900_to_first_frame_written_ms"
                ] = (
                    writer.first_written_lsl_s
                    - controller.start_mapped_local_s
                ) * 1000.0

        json_path.write_text(
            json.dumps(summary, indent=2),
            encoding="utf-8",
        )
        print(f"  Summary              : {json_path}")

        return 0

    finally:
        shutdown.set()
        writer_stop.set()

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

        if preview:
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


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Minimal BU238MCF 30 FPS LSL camera recorder."
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=Path("camera_settings.json"),
    )
    parser.add_argument(
        "--mode",
        choices=["manual", "ptb"],
        required=True,
    )
    parser.add_argument("--subject", required=True)
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Display a reduced live preview. Use mainly in manual mode.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        settings = load_settings(args.settings.resolve())
        sys.exit(
            run(
                settings=settings,
                subject=args.subject.strip(),
                mode=args.mode,
                preview=bool(args.preview),
            )
        )
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print(f"\n[FATAL] {exc}", file=sys.stderr)
        sys.exit(1)
