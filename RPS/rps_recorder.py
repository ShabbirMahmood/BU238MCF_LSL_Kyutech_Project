#!/usr/bin/env python3
"""RPS-only manual recorder: Enter starts once; Q stops, drains, saves and exits.
Run through ../run_RPS.bat. Uses the existing repo's camera helper functions.
No PTB inlet, trigger 900/999 control, calibration, or automatic white balance.
"""
from __future__ import annotations

import argparse
import csv
import importlib.metadata
import json
import logging
import os
import platform
import queue
import shutil
import signal
import sys
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from rps_common import FrameQueue, Gate, Shared, load_config, safe_name

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
TITLE = "RPS Teli Camera | Enter: Start | Q: Stop And Save"


def environment(cfg, camera_check=True):
    from rps_camera import load_existing_helpers
    helpers, source = load_existing_helpers(REPO)
    import cv2
    import numpy as np
    import pylsl
    if platform.architecture()[0] != "64bit":
        raise RuntimeError("Use 64-bit Python with the x64 Teli GenTL producer.")
    if camera_check and not Path(cfg["cti_path"]).is_file():
        raise FileNotFoundError(f"Teli CTI not found: {cfg['cti_path']}")
    versions = {}
    for name in ("harvesters", "genicam", "numpy", "opencv-python", "pylsl"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "Not reported"
    source.update(python=sys.version, packages=versions)
    return helpers, source, cv2, np, pylsl


def create_outlets(cfg, actual, run_id, pylsl):
    # Irregular metadata avoids suggesting that host-delivery timestamps are
    # an exact equally spaced camera exposure clock. FPS remains in metadata.
    info = pylsl.StreamInfo(cfg["frame_stream_name"], "VideoFrameMetadata", 3, 0.0,
                           "double64", f"RPS_Teli_{actual['serial']}_{run_id}_frames")
    desc = info.desc()
    desc.append_child_value("experiment", "RPS")
    desc.append_child_value("subject_id", cfg["subject_id"])
    desc.append_child_value("camera_serial", actual["serial"])
    desc.append_child_value("camera_fps_readback", str(actual["fps"]))
    desc.append_child_value("timestamp_meaning", "Camera-PC LSL local_clock immediately after completed buffer fetch; NOT exposure onset")
    desc.append_child_value("sample_scope", "Recording frames accepted into writer queue only; preview frames not transmitted")
    desc.append_child_value("write_acknowledgement", "No. Confirm against frame CSV and final summary")
    channels = desc.append_child("channels")
    for label in ("frame_id", "video_frame_index", "dropped_frame_total"):
        ch = channels.append_child("channel")
        ch.append_child_value("label", label)
        ch.append_child_value("unit", "count")
    events = pylsl.StreamInfo(cfg["event_stream_name"], "Markers", 1, 0.0, "string",
                              f"RPS_Teli_{actual['serial']}_{run_id}_events")
    events.desc().append_child_value("subject_id", cfg["subject_id"])
    events.desc().append_child_value("experiment", "RPS")
    return (pylsl.StreamOutlet(info, chunk_size=1, max_buffered=360),
            pylsl.StreamOutlet(events, chunk_size=1, max_buffered=360))


def elapsed_text(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds//3600:02d}:{seconds//60%60:02d}:{seconds%60:02d}"


def poll_console():
    """Windows CMD keys, only when this console has focus. No global hooks."""
    if os.name != "nt" or not sys.stdin.isatty():
        return []
    import msvcrt
    result = []
    for _ in range(32):
        if not msvcrt.kbhit():
            break
        c = msvcrt.getwch()
        if c in ("\x00", "\xe0"):
            msvcrt.getwch()  # Consume the special-key suffix, not an action.
            continue
        result.append(ord(c))
    return result


def print_help():
    print("[Keys] Enter: Start Once | Q: Stop, Save And Exit | H: Help | P: Toggle Preview", flush=True)
    print("[Focus] Click the preview or this CMD window first. Keys are NOT global game controls.", flush=True)


def draw_preview(cv2, np, image, gate, stats, cfg, actual, queue_depth, clock):
    if image is None:
        width = cfg["preview_width"]
        image = np.zeros((max(180, round(width*actual["height"]/actual["width"])), width, 3), np.uint8)
    panel = image.copy()  # Never put preview text on the saved camera image.
    state = gate["state"]
    duration = 0 if gate["start_s"] is None else ((gate["stop_s"] or clock())-gate["start_s"])
    text = [f"{state} | {cfg['subject_id']} | {elapsed_text(duration)}",
            f"Queue {queue_depth}/{cfg['writer_queue_frames']} | Written {stats['written']} | ID gaps {stats['camera_gaps']}",
            "Enter: Start | Q: Stop And Save | H: Help | P: Preview"]
    cv2.rectangle(panel, (0, 0), (panel.shape[1], 92), (0, 0, 0), -1)
    color = (40, 220, 40) if state == "Recording" else (0, 210, 255)
    scale = min(0.62, max(0.33, panel.shape[1]/1550))
    for i, line in enumerate(text):
        cv2.putText(panel, line, (10, 24+i*28), cv2.FONT_HERSHEY_SIMPLEX, scale,
                    color if i == 0 else (240, 240, 240), 1, cv2.LINE_AA)
    cv2.imshow(TITLE, panel)


def check_installation(cfg):
    helpers, source, cv2, np, pylsl = environment(cfg)
    from rps_workers import make_sink
    print(f"[Python] {sys.executable}")
    print(f"[Repo] {source['source_file']}")
    print(f"[Packages] {source['packages']}")
    print(f"[CTI] {cfg['cti_path']}")
    print(f"[LSL] Version {pylsl.library_version()}")
    # Small REAL encoding/decoding test in an automatically cleaned temp dir.
    # This does not open the camera or advertise a stream.
    actual = {"width": 160, "height": 120, "fps": cfg["fps"]}
    with tempfile.TemporaryDirectory(prefix="RPS_writer_check_") as tmp:
        path = Path(tmp)/("test.mkv" if cfg["video_backend"] == "ffmpeg" else "test.avi")
        sink = make_sink(path, cfg, actual)
        try:
            for i in range(6):
                sink.write(np.full((120, 160, 3), i*25, np.uint8))
        finally:
            sink.close()
        cap = cv2.VideoCapture(str(path))
        n = 0
        try:
            while True:
                ok, _ = cap.read()
                if not ok:
                    break
                n += 1
        finally:
            cap.release()
        if n != 6:
            raise RuntimeError(f"Writer smoke test decoded {n}/6 frames.")
    print("[Pass] Imports, CTI existence, LSL library and six-frame writer round-trip.")
    print("[Not Tested] USB driver loading, actual camera parameters, live preview and recording throughput.")


def run(cfg):
    from rps_camera import TeliCamera
    from rps_workers import Acquisition, Events, Writer
    helpers, source, cv2, np, pylsl = environment(cfg)
    cv2.setNumThreads(1)  # Avoid independent OpenCV pools competing for all CPU cores.
    logging.getLogger().setLevel(logging.WARNING)
    clock = pylsl.local_clock
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:6]
    output = Path(cfg["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(output).free < cfg["min_free_space_gb"]*1e9:
        raise RuntimeError("Not enough free disk space. Check output_dir and min_free_space_gb.")
    folder = output/f"RPS_{safe_name(cfg['subject_id'])}_{run_id}"
    folder.mkdir(exist_ok=False)
    video_path = folder/("video.mkv" if cfg["video_backend"] == "ffmpeg" else "video.avi")
    csv_path, event_path, summary_path = folder/"frames.csv", folder/"events.csv", folder/"summary.json"
    (folder/"config_used.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    summary = dict(run_id=run_id, subject_id=cfg["subject_id"], status="Initializing",
                   started_utc=datetime.now(timezone.utc).isoformat(), source=source,
                   files=dict(video=str(video_path), frames=str(csv_path), events=str(event_path)),
                   timestamp_meaning="Host completed-frame fetch time; not exposure onset",
                   frame_metadata_scope="Writer-queue accepted frames; not a disk or LabRecorder acknowledgement",
                   video_timing="Constant-frame-rate sequential playback. Use frame index + CSV/XDF for real timing; no gap padding.")
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    gate, shared, events = Gate(cfg["post_stop_seconds"]), Shared(), Events()
    frame_queue = FrameQueue(maxsize=cfg["writer_queue_frames"])
    producer_done, force_stop, signal_stop = threading.Event(), threading.Event(), threading.Event()
    camera = writer = acq = metadata = event_outlet = journal = None
    camera_owned_by_worker = False
    gui_open = False
    event_fail = None
    old_signals = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            old_signals[sig] = signal.signal(sig, lambda *_: signal_stop.set())
        except (ValueError, OSError):
            pass

    def drain_events():
        nonlocal event_fail
        while True:
            try:
                name, ts, detail = events.q.get_nowait()
            except queue.Empty:
                break
            print(f"[{name}] {detail}".rstrip(), flush=True)
            try:
                event_writer.writerow([f"{ts:.9f}", name, detail])
                journal.flush()
                if event_outlet is not None:
                    event_outlet.push_sample([name + ("|"+detail if detail else "")], timestamp=float(ts))
            except Exception as exc:
                event_fail = str(exc)
                shared.fail(f"Event journal/LSL failure: {exc}")

    try:
        journal = event_path.open("x", newline="", encoding="utf-8")
        event_writer = csv.writer(journal)
        event_writer.writerow(["host_lsl_timestamp_s", "event", "detail"])
        print("[Init] Loading Toshiba Teli camera. Close TeliViewer and other camera apps.", flush=True)
        camera = TeliCamera(cfg, helpers, clock)
        actual = camera.actual
        summary["camera_readback"] = actual
        metadata, event_outlet = create_outlets(cfg, actual, run_id, pylsl)
        print(f"[LSL] {cfg['frame_stream_name']} | {cfg['event_stream_name']}", flush=True)
        print(f"[Camera] {actual['width']}x{actual['height']} | Requested {cfg['fps']:.3f} FPS | Readback {actual['fps']:.6f} FPS")
        print(f"[Image] Exposure {actual['exposure_us']:.1f} us | Gain {actual['gain_db']:.2f} | Gamma {actual['gamma']:.2f}")
        print(f"[Buffers] Camera slots {actual['camera_buffers']} | Writer queue {cfg['writer_queue_frames']} frames")
        memory_mb = actual['width']*actual['height']*(actual['camera_buffers']+cfg['writer_queue_frames'])/1e6
        print(f"[Buffers] Approximate raw-buffer capacity {memory_mb:.0f} MB; other Python/encoder memory is additional.")
        print(f"[Output] {folder}", flush=True)
        print_help()
        events.put("CAMERA_STREAM_READY", clock(), "Outlets advertised; preview-only frames are not saved or streamed.")
        drain_events()
        writer = Writer(cfg, actual, frame_queue, producer_done, shared, events, clock,
                        helpers.raw_to_bgr, video_path, csv_path)
        writer.start()
        if not writer.ready.wait(15):
            raise RuntimeError("Video writer did not initialize within 15 seconds.")
        if shared.snapshot()["error"]:
            raise RuntimeError(shared.snapshot()["error"])
        if cfg["preview_enabled"]:
            cv2.namedWindow(TITLE, cv2.WINDOW_NORMAL)
            gui_open = True
            cv2.resizeWindow(TITLE, cfg["preview_width"],
                             round(cfg["preview_width"]*actual["height"]/actual["width"]))
            draw_preview(cv2, np, None, gate.snapshot(), shared.snapshot(), cfg, actual, 0, clock)
            cv2.waitKey(1)
        acq = Acquisition(cfg, camera, gate, shared, frame_queue, producer_done, force_stop, events, metadata, clock)
        acq.start()
        camera_owned_by_worker = True
        preview_live = cfg["preview_enabled"]
        image = None
        last_status = last_draw = time.monotonic()
        prior_received = prior_written = 0
        stopping_since = None
        queue_warned = False
        window_shown = gui_open
        while True:
            drain_events()
            now = clock()
            stats, gs = shared.snapshot(), gate.snapshot()
            keys = poll_console()
            if gui_open:
                key = cv2.waitKey(1)
                if key >= 0:
                    keys.append(key & 0xFF)
                try:
                    closed = window_shown and cv2.getWindowProperty(TITLE, cv2.WND_PROP_VISIBLE) < 1
                except cv2.error:
                    closed = True
                if closed:
                    keys.append(ord('q'))
                    gui_open = False
            if signal_stop.is_set():
                keys.append(ord('q'))
            for key in keys:
                if key in (13, 10):
                    if gate.snapshot()["state"] != "Ready":
                        print(f"[Ignored] Enter in state {gate.snapshot()['state']}; recording is not restarted.")
                        continue
                    consumer = metadata.have_consumers()
                    if not consumer and cfg["require_lsl_consumer_before_start"]:
                        print("[Warning] No metadata consumer. Start LabRecorder, then press Enter again.")
                        continue
                    if not consumer:
                        print("[Warning] No metadata consumer; local video will record but XDF recording is not confirmed.")
                    t = clock()
                    if gate.start(t):
                        events.put("RECORDING_STARTED", t, "Enter accepted; awaiting the next eligible delivered frame.")
                elif key in (ord('q'), ord('Q'), 27):
                    t = clock()
                    if gate.stop(t, "User Q/Escape/Ctrl+C"):
                        events.put("STOP_REQUEST_RECEIVED", t, f"Tail={cfg['post_stop_seconds']:.3f}s; drain queued frames and exit.")
                elif key in (ord('h'), ord('H')):
                    print_help()
                elif key in (ord('p'), ord('P')) and gui_open:
                    preview_live = not preview_live
                    print(f"[Preview] {'Live' if preview_live else 'Paused; recording continues'}")
            stats, gs = shared.snapshot(), gate.snapshot()
            if stats["error"]:
                force_stop.set()
                gate.stop(clock(), "Error", immediate=True)
            if producer_done.is_set() and gs["stop_s"] is None:
                gate.stop(clock(), "Acquisition ended", immediate=True)
            gs = gate.snapshot()
            if gs["stop_s"] is not None and stopping_since is None:
                stopping_since = time.monotonic()
            if producer_done.is_set() and writer.done.is_set():
                break
            if stopping_since is not None and time.monotonic()-stopping_since > cfg["writer_shutdown_timeout_seconds"] + cfg["post_stop_seconds"]:
                shared.fail("Shutdown/draining timed out. File may be incomplete; do not use this recording without inspection.")
                force_stop.set()
                if writer.sink is not None:
                    writer.sink.abort()
                break
            if gui_open and time.monotonic()-last_draw >= 1/cfg["preview_fps"]:
                candidate = shared.take_preview()
                if candidate is not None and preview_live:
                    _, _, raw, fmt = candidate
                    bgr = helpers.raw_to_bgr(raw, fmt)
                    width = min(cfg["preview_width"], bgr.shape[1])
                    image = cv2.resize(bgr, (width, round(width*bgr.shape[0]/bgr.shape[1])), interpolation=cv2.INTER_AREA)
                draw_preview(cv2, np, image, gs, stats, cfg, actual, frame_queue.qsize(), clock)
                last_draw = time.monotonic()
            elapsed_status = time.monotonic()-last_status
            if elapsed_status >= cfg["status_interval_seconds"]:
                depth = frame_queue.qsize()
                capture_fps = (stats["received"]-prior_received)/elapsed_status
                write_fps = (stats["written"]-prior_written)/elapsed_status
                elapsed = 0 if gs["start_s"] is None else (gs["stop_s"] or now)-gs["start_s"]
                percent = depth/cfg["writer_queue_frames"]
                print(f"[{gs['state']}] {elapsed_text(elapsed)} | Receive {capture_fps:.1f} FPS | Write {write_fps:.1f} FPS | "
                      f"Frames {stats['accepted']}/{stats['written']} queued/written | Queue {depth}/{cfg['writer_queue_frames']} "
                      f"({100*percent:.0f}%, ~{depth/actual['fps']:.2f}s) | ID gaps {stats['camera_gaps']} | Timeouts {stats['fetch_timeouts']}", flush=True)
                if percent >= cfg["queue_warning_fraction"] and not queue_warned:
                    events.put("QUEUE_WARNING", now, "Writer falling behind. P pauses preview. Reduce FPS/ROI or change encoder before next run.")
                    queue_warned = True
                elif percent < cfg["queue_warning_fraction"]*0.75:
                    queue_warned = False
                if gs["start_s"] is not None and gs["stop_s"] is None and shutil.disk_usage(folder).free < cfg["min_free_space_gb"]*1e9:
                    shared.fail("Free disk space fell below min_free_space_gb. Stopping before the disk fills.")
                    force_stop.set()
                    gate.stop(clock(), "Low disk space", immediate=True)
                prior_received, prior_written = stats["received"], stats["written"]
                last_status = time.monotonic()
            time.sleep(0.003)
    except BaseException as exc:
        shared.fail(f"{type(exc).__name__}: {exc}")
        print(f"[Error] {exc}", file=sys.stderr, flush=True)
        (folder/"error.txt").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        force_stop.set()
        gate.stop(clock(), "Cleanup", immediate=True)
        # Producer owns stop/destroy once started. Do not touch the native
        # camera concurrently with fetch().
        if acq is not None and camera_owned_by_worker:
            acq.join(timeout=max(3, cfg["fetch_timeout_seconds"]+2))
            if acq.is_alive():
                shared.fail("Camera worker did not exit. Driver may be blocked.")
        else:
            if camera is not None:
                for msg in camera.close():
                    shared.fail(msg)
            producer_done.set()
        if writer is not None:
            deadline = time.monotonic()+cfg["writer_shutdown_timeout_seconds"]
            while writer.is_alive() and time.monotonic() < deadline:
                writer.join(timeout=0.5)
                if journal is not None:
                    drain_events()
            if writer.is_alive():
                shared.fail("Writer did not finalize. Video is unverified/incomplete.")
                if writer.sink is not None:
                    writer.sink.abort()
                writer.join(timeout=3)
        if gui_open:
            try:
                cv2.destroyWindow(TITLE)
            except cv2.error:
                pass
        stats, gs = shared.snapshot(), gate.snapshot()
        mismatch = len({stats[k] for k in ("accepted", "written", "csv_rows", "lsl_samples")}) != 1
        if mismatch:
            shared.fail("Frame-count mismatch among accepted frames, write calls, CSV rows and LSL samples.")
        if gs["start_s"] is not None and stats["written"] == 0 and not stats["error"]:
            shared.fail("Start was requested but no video frame was written.")
        if journal is not None:
            drain_events()
        stats = shared.snapshot()
        if stats["error"]:
            status = "FailedOrIncomplete"
        elif gs["start_s"] is None:
            status = "CancelledBeforeRecording"
        elif stats["camera_gaps"] or stats["fetch_timeouts"]:
            status = "FinishedWithWarnings"
        else:
            status = "Finished"
        if status == "CancelledBeforeRecording" and video_path.is_file() and (writer is None or writer.done.is_set()):
            video_path.unlink()  # Only this run's empty video; never touches prior recordings.
        summary.update(status=status, finished_utc=datetime.now(timezone.utc).isoformat(),
                       controls=gs, counts_and_timing=stats,
                       queue_remaining=frame_queue.qsize(),
                       counts_match=not mismatch, writer_thread_finished=writer is not None and writer.done.is_set(),
                       writer_finalized=writer is not None and writer.finalized,
                       event_logging_error=event_fail)
        if gs["start_s"] is not None and stats["first_delivery_s"] is not None:
            summary["enter_to_first_completed_frame_delivery_ms"] = 1000*(stats["first_delivery_s"]-gs["start_s"])
        if stats["first_delivery_s"] is not None and stats["last_delivery_s"] > stats["first_delivery_s"]:
            summary["recorded_delivery_fps"] = (stats["accepted"]-1)/(stats["last_delivery_s"]-stats["first_delivery_s"])
        if journal is not None:
            events.put("CAMERA_RECORDING_STOPPED" if status in ("Finished", "FinishedWithWarnings") else "CAMERA_RUN_ENDED",
                       clock(), f"Status={status}; Frames={stats['written']}; IDGaps={stats['camera_gaps']}")
            drain_events()
            if shared.snapshot()["error"] and not stats["error"]:
                status = summary["status"] = "FailedOrIncomplete"
                summary["counts_and_timing"] = shared.snapshot()
            try:
                journal.flush()
                os.fsync(journal.fileno())
            finally:
                journal.close()
        summary["event_logging_error"] = event_fail
        temporary_summary = summary_path.with_suffix(".json.tmp")
        temporary_summary.write_text(json.dumps(summary, indent=2, allow_nan=False), encoding="utf-8")
        temporary_summary.replace(summary_path)
        print(f"\n[Final] {status} | Queued={stats['accepted']} | WriteCalls={stats['written']} | CSV={stats['csv_rows']} | LSL={stats['lsl_samples']}")
        print(f"[Final] ID gaps={stats['camera_gaps']} | Peak queue={stats['queue_peak']}/{cfg['writer_queue_frames']}")
        if "enter_to_first_completed_frame_delivery_ms" in summary:
            print(f"[Timing] Enter -> first completed-frame delivery: {summary['enter_to_first_completed_frame_delivery_ms']:.3f} ms (not exposure onset)")
        print(f"[Saved] {folder}")
        print("[LabRecorder] Keep recording through finalization; stop LabRecorder after this program finishes.")
        if event_outlet is not None:
            time.sleep(cfg["lsl_linger_seconds"])
        for sig, old in old_signals.items():
            signal.signal(sig, old)
    return 1 if summary["status"] == "FailedOrIncomplete" else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=HERE/"rps_config.json")
    parser.add_argument("--subject", help="Override subject_id for this run")
    parser.add_argument("--check", action="store_true", help="Dependencies + tiny encoder test; no camera acquisition")
    parser.add_argument("--self-test", action="store_true", help="Hardware-free unit/integration tests")
    args = parser.parse_args()
    if args.self_test:
        import unittest
        suite = unittest.defaultTestLoader.discover(str(HERE), pattern="test_rps.py")
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    cfg = load_config(args.config)
    if args.subject is not None:
        if not args.subject.strip():
            raise ValueError("Subject cannot be empty.")
        cfg["subject_id"] = args.subject.strip()
    if args.check:
        check_installation(cfg)
        return 0
    return run(cfg)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"[Startup Error] {type(exc).__name__}: {exc}", file=sys.stderr)
        print("Run run_RPS.bat --check and verify RPS/rps_config.json.", file=sys.stderr)
        sys.exit(1)
