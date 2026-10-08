"""Small, testable state/configuration helpers. No camera or LSL imports."""
from __future__ import annotations

import json
import math
import queue
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def load_config(path: Path) -> dict[str, Any]:
    """All relative file paths are relative to the CONFIG file, not the CMD cwd."""
    path = path.resolve()
    cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    template = json.loads(Path(__file__).with_name("rps_config.json").read_text(encoding="utf-8-sig"))
    missing, extra = set(template) - set(cfg), set(cfg) - set(template)
    if missing or extra:
        raise ValueError(f"Config keys: missing={sorted(missing)}; unknown={sorted(extra)}")
    validate_config(cfg)
    for key in ("cti_path", "output_dir"):
        p = Path(cfg[key]).expanduser()
        cfg[key] = str(p.resolve() if p.is_absolute() else (path.parent / p).resolve())
    exe = cfg["ffmpeg_path"]
    if exe != "ffmpeg" and not Path(exe).is_absolute():
        cfg["ffmpeg_path"] = str((path.parent / exe).resolve())
    cfg["config_file"] = str(path)
    return cfg


def validate_config(cfg: dict) -> None:
    positive = ("fps", "exposure_us", "gamma", "balance_red", "balance_blue",
                "fetch_timeout_seconds", "camera_stall_timeout_seconds", "preview_fps",
                "status_interval_seconds", "writer_shutdown_timeout_seconds")
    nonnegative = ("gain_db", "black_level", "warmup_seconds", "post_stop_seconds",
                   "min_free_space_gb", "lsl_linger_seconds")
    integers = {"width": 16, "height": 16, "offset_x": 0, "offset_y": 0,
                "camera_buffers": 8, "writer_queue_frames": 2,
                "preview_width": 320, "discard_frames_after_start": 0, "ffmpeg_crf": 0}
    for key in positive + nonnegative:
        v = cfg[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"{key} must be a finite number.")
        if v < 0 or (key in positive and v == 0):
            raise ValueError(f"Invalid {key}={v}.")
    for key, minimum in integers.items():
        if type(cfg[key]) is not int or cfg[key] < minimum:
            raise ValueError(f"{key} must be an integer >= {minimum}.")
    for key in ("preview_enabled", "require_lsl_consumer_before_start"):
        if type(cfg[key]) is not bool:
            raise ValueError(f"{key} must be JSON true/false, not text.")
    if not isinstance(cfg["queue_warning_fraction"], (int, float)) or not 0 < cfg["queue_warning_fraction"] < 1:
        raise ValueError("queue_warning_fraction must be between 0 and 1.")
    if cfg["exposure_us"] >= 1e6 / cfg["fps"]:
        raise ValueError("Exposure must be shorter than one requested frame period (1e6/fps microseconds).")
    if cfg["camera_stall_timeout_seconds"] < cfg["fetch_timeout_seconds"]:
        raise ValueError("camera_stall_timeout_seconds must be >= fetch_timeout_seconds.")
    if cfg["pixel_format"] not in ("Mono8", "BayerBG8", "BayerGB8", "BayerGR8", "BayerRG8"):
        raise ValueError("Use Mono8 or a supported 8-bit Bayer pixel format.")
    if any(cfg[k] % 2 for k in ("width", "height", "offset_x", "offset_y")):
        raise ValueError("Use even dimensions/offsets to preserve Bayer parity and support YUV420 encoding.")
    if cfg["video_backend"] not in ("opencv", "ffmpeg"):
        raise ValueError("video_backend must be opencv or ffmpeg.")
    if not isinstance(cfg["video_codec"], str) or len(cfg["video_codec"]) != 4:
        raise ValueError("video_codec must be four characters, e.g. MJPG.")
    if not 0 <= cfg["ffmpeg_crf"] <= 51:
        raise ValueError("ffmpeg_crf must be in 0..51.")
    if cfg["ffmpeg_preset"] not in ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"):
        raise ValueError("Unknown x264 preset.")
    for key in ("subject_id", "cti_path", "output_dir", "ffmpeg_path", "frame_stream_name", "event_stream_name"):
        if not isinstance(cfg[key], str) or not cfg[key].strip():
            raise ValueError(f"{key} must be nonempty text.")
    if not isinstance(cfg["serial"], str):
        raise ValueError('serial must be quoted text, e.g. "1205610" or "".')
    if cfg["frame_stream_name"] == cfg["event_stream_name"]:
        raise ValueError("Frame and event stream names must differ.")


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text.strip()).strip("_")[:64] or "Subject"


class Gate:
    """A single run: Warmup -> Ready -> Recording -> Stopping. No restarts."""
    def __init__(self, post_stop_seconds: float = 0):
        self.lock = threading.Lock()
        self.ready = False
        self.start_s: float | None = None
        self.stop_s: float | None = None
        self.deadline_s: float | None = None
        self.stop_reason = ""
        self.tail = post_stop_seconds

    def mark_ready(self) -> None:
        with self.lock:
            self.ready = True

    def start(self, t: float) -> bool:
        with self.lock:
            if not self.ready or self.start_s is not None or self.stop_s is not None:
                return False
            self.start_s = t
            return True

    def stop(self, t: float, reason: str, immediate: bool = False) -> bool:
        with self.lock:
            if self.stop_s is not None:
                return False
            self.stop_s, self.stop_reason = t, reason
            self.deadline_s = t + (self.tail if self.start_s is not None and not immediate else 0)
            return True

    def snapshot(self) -> dict:
        with self.lock:
            state = ("Stopping" if self.stop_s is not None else
                     "Recording" if self.start_s is not None else
                     "Ready" if self.ready else "Warmup")
            return dict(state=state, ready=self.ready, start_s=self.start_s,
                        stop_s=self.stop_s, deadline_s=self.deadline_s, stop_reason=self.stop_reason)

    def includes(self, delivered_s: float) -> bool:
        g = self.snapshot()
        return g["start_s"] is not None and delivered_s >= g["start_s"] and (
            g["deadline_s"] is None or delivered_s < g["deadline_s"])


class FrameIDs:
    """Missing IDs are inferred gaps, not a diagnosis of where loss occurred."""
    def __init__(self):
        self.previous: int | None = None
        self.total_missing = 0

    def add(self, value: int) -> int:
        if type(value) is not int or not 0 <= value <= 2**53:
            raise RuntimeError("Camera BlockID is invalid or cannot be represented exactly in double64.")
        if self.previous is not None and value <= self.previous:
            raise RuntimeError(f"Non-increasing camera BlockID: {self.previous} -> {value}. No frame renumbering.")
        missing = 0 if self.previous is None else value - self.previous - 1
        self.previous = value
        self.total_missing += missing
        return missing


@dataclass(frozen=True)
class Packet:
    index: int
    frame_id: int
    delivered_s: float
    raw: Any
    pixel_format: str
    interval_ms: float | None
    missing: int
    missing_total: int


class Shared:
    """Short-lock statistics and a one-frame preview slot, not a second recording queue."""
    def __init__(self):
        self.lock = threading.Lock()
        self.data = dict(received=0, accepted=0, written=0, csv_rows=0, lsl_samples=0,
                         camera_gaps=0, fetch_timeouts=0, queue_rejected=0, queue_peak=0,
                         discarded_after_start=0, first_delivery_s=None, last_delivery_s=None,
                         first_write_return_s=None, last_write_return_s=None,
                         error=None, acquisition_started_s=None, acquisition_stopped_s=None)
        self.preview = None

    def set(self, **kwargs) -> None:
        with self.lock:
            self.data.update(kwargs)

    def add(self, **kwargs) -> None:
        with self.lock:
            for k, v in kwargs.items():
                self.data[k] += v

    def fail(self, message: str) -> None:
        with self.lock:
            if self.data["error"] is None:
                self.data["error"] = message

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.data)

    def put_preview(self, value) -> None:
        with self.lock:
            self.preview = value

    def take_preview(self):
        with self.lock:
            value, self.preview = self.preview, None
            return value


class FrameQueue(queue.Queue):
    """Measure peak queued frames under the same lock as Queue.put/get."""
    def __init__(self, maxsize):
        super().__init__(maxsize)
        self._peak = 0

    def _put(self, item):
        super()._put(item)
        self._peak = max(self._peak, self._qsize())

    @property
    def peak(self):
        with self.mutex:
            return self._peak
