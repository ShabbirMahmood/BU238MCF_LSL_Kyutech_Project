"""Teli adapter. Reuses buffer copying and colour conversion from the user's repo.
No file in the parent project is changed, and no FIXED_FPS constant is patched.
"""
from __future__ import annotations

import hashlib
import importlib.util
import math
import sys
from pathlib import Path


def load_existing_helpers(repo: Path):
    source = repo / "bu238mcf_lsl_30fps.py"
    if not source.is_file():
        raise FileNotFoundError(f"Keep RPS/ inside the repo. Required file not found: {source}")
    name = "_rps_existing_teli_helpers"
    spec = importlib.util.spec_from_file_location(name, source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {source}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # Required by dataclass/type processing.
    spec.loader.exec_module(module)  # The repo's __main__ entry point does not run.
    names = ("Harvester", "TimeoutException", "copy_raw_component", "camera_frame_id", "raw_to_bgr")
    absent = [n for n in names if not hasattr(module, n)]
    if absent:
        raise RuntimeError(f"Repo helper API changed: missing {absent}. Use the reviewed version or send your Python file.")
    return module, {"source_file": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}


def node_value(nm, name, default=None):
    try:
        return getattr(nm, name).value
    except Exception:
        return default


def set_node(nm, name: str, value, *, optional: bool = False):
    """No silent range clamping. Readback is retained for the final summary."""
    try:
        node = getattr(nm, name)
    except Exception as exc:
        if optional:
            return None
        raise RuntimeError(f"Required GenICam node is missing: {name}") from exc
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                minimum, maximum = float(node.min), float(node.max)
            except Exception:
                minimum, maximum = -math.inf, math.inf
            if not minimum <= value <= maximum:
                raise ValueError(f"Requested {value}, supported range [{minimum}, {maximum}]")
            if isinstance(value, int):
                try:
                    inc = int(node.inc)
                    base = int(node.min)
                except Exception:
                    inc, base = 1, 0
                if inc > 0 and (value-base) % inc:
                    raise ValueError(f"Requested {value}; value must be {base} + N*{inc}")
        node.value = value
        actual = node.value
        if isinstance(value, (str, bool, int)) and actual != value:
            raise ValueError(f"Readback {actual!r} differs from request {value!r}")
        return actual
    except Exception as exc:
        # Even optional nodes are not silently ignored if they are present
        # but remain in an unwanted automatic mode.
        try:
            if node.value == value:
                return node.value
        except Exception:
            pass
        raise RuntimeError(f"Cannot set {name}={value!r}: {exc}") from exc


class TeliCamera:
    def __init__(self, cfg: dict, helpers, clock):
        self.cfg, self.helpers, self.clock = cfg, helpers, clock
        self.timeout_error = helpers.TimeoutException
        self.h = helpers.Harvester()
        self.ia = None
        self.actual = {}
        try:
            if not Path(cfg["cti_path"]).is_file():
                raise FileNotFoundError(f"Teli CTI file not found: {cfg['cti_path']}")
            self.h.add_file(cfg["cti_path"])
            self.h.update()
            if not self.h.device_info_list:
                raise RuntimeError("No camera found. Check USB3/TeliViewer, then CLOSE TeliViewer.")
            if not cfg["serial"] and len(self.h.device_info_list) != 1:
                raise RuntimeError("Multiple cameras found: set serial in rps_config.json.")
            self.ia = self.h.create({"serial_number": cfg["serial"]}) if cfg["serial"] else self.h.create(0)
            self.configure()
        except BaseException:
            self.close()
            raise

    def configure(self):
        c, nm = self.cfg, self.ia.remote_device.node_map
        set_node(nm, "AcquisitionMode", "Continuous")
        set_node(nm, "TriggerMode", "Off")
        set_node(nm, "OffsetX", 0, optional=True)
        set_node(nm, "OffsetY", 0, optional=True)
        set_node(nm, "Width", c["width"])
        set_node(nm, "Height", c["height"])
        set_node(nm, "OffsetX", c["offset_x"], optional=c["offset_x"] == 0)
        set_node(nm, "OffsetY", c["offset_y"], optional=c["offset_y"] == 0)
        set_node(nm, "PixelFormat", c["pixel_format"])
        set_node(nm, "ReverseX", False, optional=True)
        set_node(nm, "ReverseY", False, optional=True)
        set_node(nm, "ExposureAuto", "Off", optional=True)
        set_node(nm, "ExposureTimeControl", "Manual", optional=True)
        set_node(nm, "ExposureTime", float(c["exposure_us"]))
        set_node(nm, "GainAuto", "Off", optional=True)
        set_node(nm, "Gain", float(c["gain_db"]))
        set_node(nm, "Gamma", float(c["gamma"]))
        set_node(nm, "BlackLevel", float(c["black_level"]))
        if c["pixel_format"] != "Mono8":
            set_node(nm, "BalanceWhiteAuto", "Off", optional=True)
            for selector, value in (("Red", c["balance_red"]), ("Blue", c["balance_blue"])):
                set_node(nm, "BalanceRatioSelector", selector)
                ratio = set_node(nm, "BalanceRatio", float(value))
                self.actual[f"balance_{selector.lower()}"] = float(ratio)
        set_node(nm, "AcquisitionFrameRateEnable", True, optional=True)
        set_node(nm, "AcquisitionFrameRate", float(c["fps"]))
        actual_fps = float(node_value(nm, "AcquisitionFrameRate"))
        if not math.isfinite(actual_fps) or actual_fps <= 0 or abs(actual_fps/c["fps"]-1) > 0.02:
            raise RuntimeError(f"FPS readback {actual_fps} is not within 2% of requested {c['fps']}. Check exposure/ROI.")
        try:
            minimum = int(self.ia.min_num_buffers)
        except Exception:
            minimum = 8
        self.ia.num_buffers = max(c["camera_buffers"], minimum)
        self.actual.update({
            "model": str(node_value(nm, "DeviceModelName", "Unknown")),
            "serial": str(node_value(nm, "DeviceSerialNumber", c["serial"])),
            "width": int(node_value(nm, "Width")), "height": int(node_value(nm, "Height")),
            "fps": actual_fps, "pixel_format": str(node_value(nm, "PixelFormat")),
            "exposure_us": float(node_value(nm, "ExposureTime")),
            "gain_db": float(node_value(nm, "Gain")), "gamma": float(node_value(nm, "Gamma")),
            "black_level": float(node_value(nm, "BlackLevel")),
            "offset_x": int(node_value(nm, "OffsetX", 0)), "offset_y": int(node_value(nm, "OffsetY", 0)),
            "camera_buffers": int(self.ia.num_buffers)})

    def start(self):
        self.ia.start()

    def fetch(self):
        with self.ia.fetch(timeout=self.cfg["fetch_timeout_seconds"]) as buffer:
            delivered = float(self.clock())  # BEFORE copy, colour conversion, preview or encoding.
            fid = self.helpers.camera_frame_id(buffer)
            raw, fmt = self.helpers.copy_raw_component(buffer.payload.components[0])
            if raw.shape != (self.actual["height"], self.actual["width"]) or fmt != self.actual["pixel_format"]:
                raise RuntimeError("Delivered frame format/dimensions changed during acquisition.")
        # GenTL buffer has been requeued BEFORE application processing.
        return fid, delivered, raw, fmt

    def close(self):
        errors = []
        if self.ia is not None:
            try:
                if self.ia.is_acquiring():
                    self.ia.stop()
            except Exception as exc:
                errors.append(f"Camera stop: {exc}")
            try:
                self.ia.destroy()
            except Exception as exc:
                errors.append(f"Camera destroy: {exc}")
            self.ia = None
        try:
            self.h.reset()
        except Exception as exc:
            errors.append(f"Harvester reset: {exc}")
        return errors
