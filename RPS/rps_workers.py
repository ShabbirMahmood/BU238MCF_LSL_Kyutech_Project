"""Separate acquisition and writing workers. OpenCV GUI stays on the main thread."""
from __future__ import annotations

import csv
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path

from rps_common import FrameIDs, Packet


class Events:
    """Workers only enqueue status events; main thread publishes/logs them."""
    def __init__(self):
        self.q = queue.Queue()

    def put(self, name: str, timestamp: float, detail: str = ""):
        self.q.put((name, timestamp, detail))


class OpenCVSink:
    def __init__(self, path: Path, cfg: dict, actual: dict):
        import cv2
        self.writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*cfg["video_codec"]),
                                      actual["fps"], (actual["width"], actual["height"]), True)
        if not self.writer.isOpened():
            self.writer.release()
            raise RuntimeError(f"Cannot open OpenCV {cfg['video_codec']} writer: {path}")

    def write(self, frame):
        self.writer.write(frame)
        # OpenCV returns no per-frame success acknowledgement.

    def close(self):
        self.writer.release()

    def abort(self):
        # Never call release() concurrently with a native write().
        pass


class FFmpegSink:
    """Optional H.264/MKV encoder. FFmpeg must be installed separately."""
    def __init__(self, path: Path, cfg: dict, actual: dict):
        self.proc = None
        self.log = None
        exe = shutil.which(cfg["ffmpeg_path"])
        if exe is None and Path(cfg["ffmpeg_path"]).is_file():
            exe = cfg["ffmpeg_path"]
        if not exe:
            raise FileNotFoundError("FFmpeg not found. Set ffmpeg_path or use video_backend=opencv.")
        self.log = path.with_suffix(".ffmpeg.log").open("xb")
        command = [exe, "-hide_banner", "-loglevel", "warning", "-nostdin", "-n",
                   "-f", "rawvideo", "-pixel_format", "bgr24", "-video_size",
                   f"{actual['width']}x{actual['height']}", "-framerate", f"{actual['fps']:.9f}",
                   "-i", "pipe:0", "-an", "-c:v", "libx264", "-preset", cfg["ffmpeg_preset"],
                   "-crf", str(cfg["ffmpeg_crf"]), "-pix_fmt", "yuv420p", "-f", "matroska", str(path)]
        try:
            self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                         stderr=self.log, bufsize=0)
        except BaseException:
            self.log.close()
            raise

    def write(self, frame):
        if self.proc.poll() is not None:
            raise RuntimeError("FFmpeg exited early. Read the session .ffmpeg.log file.")
        # An unbuffered pipe write may return a partial count. Send all bytes.
        data = memoryview(frame).cast("B")
        while data:
            n = self.proc.stdin.write(data)
            if not n:
                raise RuntimeError("FFmpeg pipe closed before the complete frame was sent.")
            data = data[n:]

    def close(self):
        try:
            try:
                self.proc.stdin.close()
            except BrokenPipeError:
                pass
            code = self.proc.wait(timeout=30)
            if code != 0:
                raise RuntimeError(f"FFmpeg exited with code {code}. Check the .ffmpeg.log file.")
        except subprocess.TimeoutExpired as exc:
            self.proc.kill()
            self.proc.wait(timeout=5)
            raise RuntimeError("FFmpeg did not finalize within 30 seconds; video may be incomplete.") from exc
        finally:
            self.log.close()

    def abort(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()


def make_sink(path, cfg, actual):
    if cfg["video_backend"] == "ffmpeg":
        return FFmpegSink(path, cfg, actual)
    return OpenCVSink(path, cfg, actual)


class Writer(threading.Thread):
    def __init__(self, cfg, actual, frame_queue, producer_done, shared, events, clock,
                 converter, video_path, csv_path, sink_factory=make_sink):
        super().__init__(name="RPSVideoWriter", daemon=True)
        self.cfg, self.actual, self.q = cfg, actual, frame_queue
        self.producer_done, self.shared, self.events, self.clock = producer_done, shared, events, clock
        self.converter, self.video_path, self.csv_path = converter, video_path, csv_path
        self.sink_factory = sink_factory
        self.ready = threading.Event()
        self.done = threading.Event()
        self.sink = None
        self.finalized = False

    def run(self):
        csv_file = None
        error = None
        written = 0
        try:
            self.sink = self.sink_factory(self.video_path, self.cfg, self.actual)
            csv_file = self.csv_path.open("x", newline="", encoding="utf-8")
            log = csv.writer(csv_file)
            log.writerow(["frame_id", "video_frame_index", "host_lsl_timestamp_s", "host_interframe_ms",
                          "dropped_this_frame", "dropped_frame_total", "writer_return_lsl_s"])
            csv_file.flush()
            self.ready.set()
            last_flush = time.monotonic()
            while not self.producer_done.is_set() or not self.q.empty():
                try:
                    p = self.q.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    if p.index != written + 1:
                        raise RuntimeError(f"Writer index discontinuity: expected {written+1}, got {p.index}")
                    frame = self.converter(p.raw, p.pixel_format)
                    if frame.dtype.name != "uint8" or frame.shape != (self.actual["height"], self.actual["width"], 3):
                        raise RuntimeError("Colour conversion returned an unexpected image format.")
                    self.sink.write(frame)
                    returned = float(self.clock())
                    written += 1
                    self.shared.set(written=written, last_write_return_s=returned)
                    if written == 1:
                        self.shared.set(first_write_return_s=returned)
                    # These rows describe write calls that returned, not disk-level ACKs.
                    log.writerow([p.frame_id, p.index, f"{p.delivered_s:.9f}",
                                  "" if p.interval_ms is None else f"{p.interval_ms:.6f}",
                                  p.missing, p.missing_total, f"{returned:.9f}"])
                    self.shared.add(csv_rows=1)
                    if written == 1 or time.monotonic() - last_flush >= 1:
                        csv_file.flush()
                        last_flush = time.monotonic()
                finally:
                    self.q.task_done()
        except BaseException as exc:
            error = f"Video writer failed: {type(exc).__name__}: {exc}"
            self.shared.fail(error)
            self.events.put("WRITER_ERROR", self.clock(), error)
        finally:
            if self.sink is not None:
                try:
                    self.sink.close()
                except BaseException as exc:
                    error = error or f"Writer finalization failed: {exc}"
                    self.shared.fail(error)
                    self.events.put("WRITER_FINALIZE_ERROR", self.clock(), str(exc))
            if csv_file is not None:
                try:
                    csv_file.flush()
                    os.fsync(csv_file.fileno())
                    csv_file.close()
                except OSError as exc:
                    error = error or f"Frame CSV flush failed: {exc}"
                    self.shared.fail(error)
            self.finalized = error is None and self.sink is not None
            self.ready.set()  # Also releases a caller waiting after startup failure.
            self.done.set()


class Acquisition(threading.Thread):
    def __init__(self, cfg, camera, gate, shared, frame_queue, producer_done, force_stop,
                 events, metadata_outlet, clock):
        super().__init__(name="RPSCameraAcquisition", daemon=True)
        self.cfg, self.camera, self.gate, self.shared = cfg, camera, gate, shared
        self.q, self.done, self.force_stop = frame_queue, producer_done, force_stop
        self.events, self.outlet, self.clock = events, metadata_outlet, clock

    def run(self):
        ids = FrameIDs()
        index = 0
        previous_host = None
        discard = self.cfg["discard_frames_after_start"]
        next_preview = -float("inf")
        last_gap_event = -float("inf")
        ready = False
        nreceived = 0
        try:
            self.camera.start()
            t0 = self.clock()
            self.shared.set(acquisition_started_s=t0)
            self.events.put("CAMERA_ACQUISITION_RUNNING", t0)
            warmup_end = t0 + self.cfg["warmup_seconds"]
            last_delivery = t0
            while not self.force_stop.is_set():
                now = self.clock()
                g = self.gate.snapshot()
                if g["deadline_s"] is not None and now >= g["deadline_s"]:
                    break
                if self.shared.snapshot()["error"]:
                    break
                try:
                    fid, delivered, raw, fmt = self.camera.fetch()
                except self.camera.timeout_error:
                    self.shared.add(fetch_timeouts=1)
                    if self.clock() - last_delivery >= self.cfg["camera_stall_timeout_seconds"]:
                        raise RuntimeError("No completed camera frame within the configured stall timeout.")
                    continue
                last_delivery = delivered
                nreceived += 1
                self.shared.set(received=nreceived)
                if not ready and delivered >= warmup_end and nreceived >= 2:
                    ready = True
                    self.gate.mark_ready()
                    self.events.put("CAMERA_READY_FOR_ENTER", delivered,
                                    "Start LabRecorder, then press Enter. Q stops and exits.")
                if self.cfg["preview_enabled"] and delivered >= next_preview:
                    self.shared.put_preview((fid, delivered, raw, fmt))
                    next_preview = delivered + 1/self.cfg["preview_fps"]
                if not self.gate.includes(delivered):
                    continue
                if discard:
                    discard -= 1
                    self.shared.add(discarded_after_start=1)
                    continue
                missing = ids.add(fid)
                self.shared.set(camera_gaps=ids.total_missing)
                p = Packet(index + 1, fid, delivered, raw, fmt,
                           None if previous_host is None else (delivered-previous_host)*1000,
                           missing, ids.total_missing)
                try:
                    self.q.put_nowait(p)  # Never wait with a GenTL buffer held.
                except queue.Full as exc:
                    self.shared.add(queue_rejected=1)
                    raise RuntimeError("Writer queue FULL. Recording stopped; already queued frames will be drained. "
                                       "Reduce FPS/ROI/preview rate or use a faster encoder/storage device.") from exc
                index += 1
                previous_host = delivered
                st = self.shared.snapshot()
                self.shared.set(accepted=index, last_delivery_s=delivered,
                                queue_peak=max(st["queue_peak"], getattr(self.q, "peak", self.q.qsize())))
                if index == 1:
                    self.shared.set(first_delivery_s=delivered)
                    self.events.put("FIRST_FRAME", delivered, f"FrameID={fid}; VideoIndex=1")
                if missing and delivered-last_gap_event >= 1:
                    self.events.put("FRAME_ID_GAP", delivered, f"MissingTotal={ids.total_missing}; FrameID={fid}")
                    last_gap_event = delivered
                # The timestamp is original HOST DELIVERY time, not encoding time.
                # A metadata sample means queued for writing, not disk confirmation.
                self.outlet.push_sample([float(fid), float(index), float(ids.total_missing)], timestamp=delivered)
                self.shared.add(lsl_samples=1)
        except BaseException as exc:
            message = f"Acquisition failed: {type(exc).__name__}: {exc}"
            self.shared.fail(message)
            self.events.put("ACQUISITION_ERROR", self.clock(), message)
        finally:
            for error in self.camera.close():
                self.shared.fail(error)
                self.events.put("CAMERA_CLOSE_ERROR", self.clock(), error)
            self.shared.set(acquisition_stopped_s=self.clock())
            self.done.set()  # Writer drains until this is true AND its queue is empty.
