"""Tests run with --self-test. No camera, GenTL DLL, LSL outlet or GUI is opened."""
from __future__ import annotations
import csv
import json
import queue
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

from rps_common import FrameIDs, FrameQueue, Gate, Packet, Shared, load_config, safe_name, validate_config
from rps_camera import set_node
from rps_workers import Acquisition, Events, FFmpegSink, OpenCVSink, Writer

HERE = Path(__file__).resolve().parent

def config():
    return json.loads((HERE/"rps_config.json").read_text())

def mono(raw, fmt):
    return np.repeat(raw[:, :, None], 3, axis=2).copy()

class MemorySink:
    def __init__(self, fail_at=None):
        self.frames = []
        self.closed = False
        self.fail_at = fail_at
    def write(self, frame):
        if self.fail_at is not None and len(self.frames) == self.fail_at:
            raise OSError("Simulated disk failure")
        self.frames.append(frame.copy())
    def close(self):
        self.closed = True
    def abort(self):
        pass

class DummyOutlet:
    def __init__(self):
        self.rows = []
    def push_sample(self, sample, timestamp):
        self.rows.append((sample, timestamp))

class DummyCamera:
    timeout_error = TimeoutError
    def __init__(self, period=.002, count=100000):
        self.frame_id = 0
        self.period, self.count = period, count
        self.closed = False
    def start(self):
        pass
    def fetch(self):
        time.sleep(self.period)
        self.frame_id += 1
        if self.frame_id > self.count:
            raise TimeoutError("Simulated timeout")
        return self.frame_id, time.monotonic(), np.full((12,16), self.frame_id % 255, np.uint8), "Mono8"
    def close(self):
        self.closed = True
        return []

class ConfigTests(unittest.TestCase):
    def test_default_valid(self):
        validate_config(config())
    def test_bad_parameters(self):
        for key, value in (("fps",0), ("fps",float('nan')), ("width",1919),
                           ("exposure_us",50000), ("serial",1205610), ("preview_enabled","true"),
                           ("writer_queue_frames",-3)):
            c=config(); c[key]=value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_config(c)
    def test_paths_relative_to_config(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"settings.json"; c=config(); c['cti_path']='sdk/cam.cti'; c['output_dir']='out'
            p.write_text(json.dumps(c))
            s=load_config(p)
            self.assertEqual(s['output_dir'],str((Path(d)/'out').resolve()))
            self.assertEqual(s['cti_path'],str((Path(d)/'sdk/cam.cti').resolve()))
    def test_safe_filename(self):
        self.assertEqual(safe_name('S 01/../'),'S_01')

class TimingTests(unittest.TestCase):
    def test_gate_and_duplicates(self):
        g=Gate()
        self.assertFalse(g.start(1))
        g.mark_ready(); self.assertTrue(g.start(10)); self.assertFalse(g.start(11))
        self.assertFalse(g.includes(9.99)); self.assertTrue(g.includes(10))
        self.assertTrue(g.stop(20,'Q')); self.assertFalse(g.stop(21,'Q'))
        self.assertTrue(g.includes(19.99)); self.assertFalse(g.includes(20))
        self.assertFalse(g.start(22))
    def test_tail_and_cancel(self):
        g=Gate(.5); g.mark_ready(); g.start(10); g.stop(11,'Q')
        self.assertTrue(g.includes(11.49)); self.assertFalse(g.includes(11.5))
        c=Gate(2); c.stop(1,'Q'); c.mark_ready()
        self.assertFalse(c.start(2)); self.assertFalse(c.includes(2))
    def test_blockid_gaps_and_reset(self):
        ids=FrameIDs()
        self.assertEqual(ids.add(500),0); self.assertEqual(ids.add(501),0)
        self.assertEqual(ids.add(505),3); self.assertEqual(ids.total_missing,3)
        with self.assertRaises(RuntimeError): ids.add(505)
        with self.assertRaises(RuntimeError): FrameIDs().add(2**53+1)

class QueueTests(unittest.TestCase):
    def test_exact_high_water(self):
        q=FrameQueue(3)
        q.put(1); q.put(2); self.assertEqual(q.peak,2)
        q.get(); q.get(); self.assertEqual(q.peak,2)
        q.put(3); q.put(4); q.put(5); self.assertEqual(q.peak,3)

class NodeTests(unittest.TestCase):
    def test_range_increment_and_optional(self):
        class N:
            min=16; max=1920; inc=4; value=1920
        class Map:
            Width=N()
        nm=Map()
        self.assertEqual(set_node(nm,'Width',1280),1280)
        with self.assertRaises(RuntimeError): set_node(nm,'Width',1282)
        with self.assertRaises(RuntimeError): set_node(nm,'Width',9999)
        self.assertIsNone(set_node(nm,'NoSuchNode',False,optional=True))
        with self.assertRaises(RuntimeError): set_node(nm,'NoSuchNode',False)

class WorkerTests(unittest.TestCase):
    def test_writer_drains_all_and_csv_matches(self):
        c=config(); actual=dict(width=16,height=12,fps=30)
        q=queue.Queue(10); done=threading.Event(); shared=Shared(); events=Events(); sink=MemorySink()
        with tempfile.TemporaryDirectory() as d:
            for i in range(1,9):
                q.put(Packet(i,100+i,10+i/30,np.full((12,16),i,np.uint8),'Mono8',None,0,0))
            w=Writer(c,actual,q,done,shared,events,time.monotonic,mono,Path(d)/'video.avi',
                     Path(d)/'frames.csv',lambda *_:sink)
            w.start(); self.assertTrue(w.ready.wait(2)); done.set(); w.join(3)
            self.assertFalse(w.is_alive()); self.assertIsNone(shared.snapshot()['error'])
            self.assertEqual(shared.snapshot()['written'],8); self.assertEqual(q.qsize(),0)
            with (Path(d)/'frames.csv').open() as f: rows=list(csv.DictReader(f))
            self.assertEqual([int(r['video_frame_index']) for r in rows],list(range(1,9)))
            self.assertEqual(sink.frames[-1][0,0,0],8); self.assertTrue(sink.closed)
    def test_writer_failure_is_not_success(self):
        c=config(); q=queue.Queue(); done=threading.Event(); done.set(); shared=Shared(); sink=MemorySink(fail_at=2)
        for i in range(1,6): q.put(Packet(i,i,float(i),np.zeros((12,16),np.uint8),'Mono8',None,0,0))
        with tempfile.TemporaryDirectory() as d:
            w=Writer(c,dict(width=16,height=12,fps=30),q,done,shared,Events(),time.monotonic,
                     mono,Path(d)/'v.avi',Path(d)/'f.csv',lambda *_:sink)
            w.start(); w.join(3)
            self.assertIn('Simulated disk failure',shared.snapshot()['error'])
            self.assertEqual(shared.snapshot()['csv_rows'],2)
    def test_live_capture_start_stop_and_metadata(self):
        c=config(); c.update(warmup_seconds=.01,discard_frames_after_start=1,preview_enabled=False)
        q=queue.Queue(100); done=threading.Event(); stop=threading.Event(); shared=Shared()
        g=Gate(); cam=DummyCamera(); outlet=DummyOutlet(); ev=Events(); sink=MemorySink()
        with tempfile.TemporaryDirectory() as d:
            writer=Writer(c,dict(width=16,height=12,fps=30),q,done,shared,ev,time.monotonic,
                          mono,Path(d)/'v.avi',Path(d)/'f.csv',lambda *_:sink)
            cap=Acquisition(c,cam,g,shared,q,done,stop,ev,outlet,time.monotonic)
            writer.start(); writer.ready.wait(2); cap.start()
            deadline=time.monotonic()+2
            while not g.snapshot()['ready'] and time.monotonic()<deadline: time.sleep(.002)
            self.assertTrue(g.snapshot()['ready']); self.assertEqual(len(outlet.rows),0)
            start=time.monotonic(); self.assertTrue(g.start(start))
            time.sleep(.08); finish=time.monotonic(); g.stop(finish,'Q')
            cap.join(3); writer.join(3)
            self.assertFalse(cap.is_alive()); self.assertFalse(writer.is_alive()); self.assertTrue(cam.closed)
            st=shared.snapshot(); self.assertIsNone(st['error']); self.assertGreater(st['written'],5)
            self.assertEqual(st['written'],st['accepted']); self.assertEqual(st['accepted'],st['lsl_samples'])
            self.assertTrue(all(start <= ts < finish for _,ts in outlet.rows))
            self.assertEqual([s[1] for s,_ in outlet.rows],list(range(1,len(outlet.rows)+1)))
    def test_queue_full_stops_without_silent_drop(self):
        c=config(); c.update(warmup_seconds=0,discard_frames_after_start=0,preview_enabled=False)
        q=queue.Queue(2); done=threading.Event(); stop=threading.Event(); shared=Shared(); g=Gate()
        g.mark_ready(); g.start(time.monotonic()); cam=DummyCamera(); outlet=DummyOutlet()
        a=Acquisition(c,cam,g,shared,q,done,stop,Events(),outlet,time.monotonic)
        a.start(); a.join(3)
        self.assertFalse(a.is_alive()); self.assertIn('queue FULL',shared.snapshot()['error'])
        self.assertEqual(len(outlet.rows),2); self.assertEqual(shared.snapshot()['queue_rejected'],1)
    def test_camera_stall_stops(self):
        c=config(); c.update(warmup_seconds=0,camera_stall_timeout_seconds=.015,preview_enabled=False)
        q=queue.Queue(100); done=threading.Event(); shared=Shared(); g=Gate()
        a=Acquisition(c,DummyCamera(count=0),g,shared,q,done,threading.Event(),Events(),DummyOutlet(),time.monotonic)
        a.start(); a.join(3)
        self.assertTrue(done.is_set()); self.assertIn('stall timeout',shared.snapshot()['error'])

class EncoderTests(unittest.TestCase):
    def _roundtrip(self, backend):
        import cv2
        c=config(); c.update(video_backend=backend,ffmpeg_path='ffmpeg')
        actual=dict(width=160,height=120,fps=15.0)
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/('video.mkv' if backend=='ffmpeg' else 'video.avi')
            sink=FFmpegSink(path,c,actual) if backend=='ffmpeg' else OpenCVSink(path,c,actual)
            try:
                for i in range(12): sink.write(np.full((120,160,3),i*15,np.uint8))
            finally: sink.close()
            cap=cv2.VideoCapture(str(path)); fps=cap.get(cv2.CAP_PROP_FPS); images=[]
            try:
                while True:
                    ok,f=cap.read()
                    if not ok: break
                    images.append(f)
            finally: cap.release()
            self.assertEqual(len(images),12); self.assertAlmostEqual(fps,15.0,places=2)
            self.assertEqual(images[0].shape,(120,160,3))
            self.assertLess(abs(float(images[-1].mean())-165),8)
    def test_opencv_real_roundtrip(self): self._roundtrip('opencv')
    @unittest.skipUnless(shutil.which('ffmpeg'), 'Optional FFmpeg is not installed')
    def test_ffmpeg_real_roundtrip(self): self._roundtrip('ffmpeg')

class ApplicationTests(unittest.TestCase):
    def test_complete_main_loop_with_simulated_camera_and_lsl(self):
        import contextlib
        import io
        import types
        from unittest.mock import patch
        import cv2
        import rps_recorder
        from rps_workers import Writer as RealWriter
        class FakeTeli(DummyCamera):
            def __init__(self, cfg, helpers, clock):
                super().__init__(period=.002)
                self.actual=dict(width=16,height=12,fps=30.0,serial="FAKE",model="TEST",
                                 exposure_us=5000.,gain_db=0.,gamma=1.,camera_buffers=8)
        class FakeOutlet(DummyOutlet):
            def have_consumers(self): return True
        frames, events=FakeOutlet(),FakeOutlet()
        sink=MemorySink()
        def writer_factory(*args, **kwargs):
            return RealWriter(*args, **kwargs, sink_factory=lambda *_:sink)
        start=time.monotonic(); started=False; stopped=False
        def key_source():
            nonlocal started,stopped
            elapsed=time.monotonic()-start
            if elapsed>.06 and not started:
                started=True; return [13]
            if elapsed>.18 and not stopped:
                stopped=True; return [ord('q')]
            if elapsed>2: return [ord('q')]
            return []
        with tempfile.TemporaryDirectory() as d:
            c=config(); c.update(output_dir=d,min_free_space_gb=0,preview_enabled=False,
                                 warmup_seconds=.01,lsl_linger_seconds=0,status_interval_seconds=.05)
            env=(types.SimpleNamespace(raw_to_bgr=mono),{},cv2,np,types.SimpleNamespace(local_clock=time.monotonic))
            with patch('rps_recorder.environment',return_value=env), \
                 patch('rps_recorder.create_outlets',return_value=(frames,events)), \
                 patch('rps_camera.TeliCamera',FakeTeli), \
                 patch('rps_workers.Writer',side_effect=writer_factory), \
                 patch('rps_recorder.poll_console',side_effect=key_source), \
                 contextlib.redirect_stdout(io.StringIO()):
                result=rps_recorder.run(c)
            self.assertEqual(result,0)
            summary=json.loads(next(Path(d).rglob('summary.json')).read_text())
            self.assertEqual(summary['status'],'Finished')
            self.assertTrue(summary['writer_finalized'])
            self.assertTrue(summary['counts_match'])
            self.assertEqual(summary['counts_and_timing']['written'],len(frames.rows))
            self.assertGreater(len(frames.rows),5)
            self.assertEqual(sum(s[0].startswith('RECORDING_STARTED') for s,_ in events.rows),1)
            self.assertEqual(sum(s[0].startswith('STOP_REQUEST_RECEIVED') for s,_ in events.rows),1)

if __name__ == '__main__':
    unittest.main(verbosity=2)
