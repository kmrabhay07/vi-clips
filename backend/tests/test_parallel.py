import threading
import wave
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor, Future
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
import numpy as np
from support import scratch
from db import Session, Job, engine, init
from sqlalchemy import text
import worker

class ParallelTests(unittest.TestCase):
    def setUp(self):
        init()
        self.ids = []

    def tearDown(self):
        with Session.begin() as db:
            for id in self.ids:
                job = db.get(Job, id)
                if job:
                    db.delete(job)

    def job(self, kind, target=None, status='queued'):
        id = uuid.uuid4().hex
        with Session.begin() as db:
            db.add(Job(id=id, kind=kind, target=target or uuid.uuid4().hex, status=status))
        self.ids.append(id)
        return id

    def test_claim_skips_second_analysis_and_busy_target(self):
        first = self.job('analyze')
        second = self.job('analyze')
        preview = self.job('preview')
        claimed = worker.claim_job([])
        self.assertIn(claimed.id, [first, second])
        next_job = worker.claim_job([claimed])
        self.assertEqual(next_job.id, preview)
        self.assertIsNone(worker.claim_job([claimed, next_job]))
        duplicate = self.job('export', next_job.target)
        self.assertIsNone(worker.claim_job([claimed, next_job]))
        self.assertEqual(worker.claim_job([claimed]).id, duplicate)

    def test_dispatch_respects_limit(self):
        for _ in range(4):
            self.job('preview')
        class PendingPool:
            def submit(self, *args):
                return Future()
        active = {}
        worker.dispatch(PendingPool(), active, 2)
        self.assertEqual(len(active), 2)
        with Session() as db:
            statuses = [db.get(Job, id).status for id in self.ids]
        self.assertEqual(statuses.count('running'), 2)
        self.assertEqual(statuses.count('queued'), 2)

    @unittest.skipUnless(engine.dialect.name == 'postgresql', 'PostgreSQL integration')
    def test_postgres_claims_are_unique_under_contention(self):
        ids = {self.job('preview') for _ in range(8)}
        barrier = threading.Barrier(8, timeout=5)
        def claim(_):
            barrier.wait()
            return worker.claim_job([]).id
        with ThreadPoolExecutor(max_workers=8) as pool:
            claimed = list(pool.map(claim, range(8)))
        self.assertEqual(set(claimed), ids)
        self.assertEqual(len(claimed), len(set(claimed)))

    @unittest.skipUnless(engine.dialect.name == 'postgresql', 'PostgreSQL integration')
    def test_postgres_coordinator_lock_excludes_other_coordinators(self):
        with engine.connect() as first, engine.connect() as second:
            try:
                self.assertTrue(first.execute(text('SELECT pg_try_advisory_lock(621901)')).scalar())
                self.assertFalse(second.execute(text('SELECT pg_try_advisory_lock(621901)')).scalar())
            finally:
                first.execute(text('SELECT pg_advisory_unlock(621901)'))

    def test_jobs_really_overlap_with_separate_sessions(self):
        ids = [self.job('preview') for _ in range(2)]
        barrier = threading.Barrier(2, timeout=5)
        sessions = []
        def fake_process(db, job):
            sessions.append(db)
            barrier.wait()  # Fails if execution is serial.
        claims = [worker.ClaimedJob(id, 'preview', '') for id in ids]
        with patch('worker.process', side_effect=fake_process):
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(worker.run_job, claims))
        self.assertEqual(len({id(s) for s in sessions}), 2)
        with Session() as db:
            self.assertTrue(all(db.get(Job, id).status == 'done' for id in ids))

    def test_failure_isolated_from_other_jobs(self):
        bad, good = self.job('preview'), self.job('preview')
        def fake_process(db, job):
            if job.id == bad:
                raise RuntimeError('render failed')
        with patch('worker.process', side_effect=fake_process):
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(worker.run_job, [worker.ClaimedJob(i, 'preview', '') for i in [bad, good]]))
        with Session() as db:
            self.assertEqual(db.get(Job, bad).status, 'failed')
            self.assertEqual(db.get(Job, good).status, 'done')

    def test_restart_recovers_interrupted_jobs(self):
        interrupted = self.job('export', status='running')
        done = self.job('preview', status='done')
        worker.recover_interrupted()
        with Session() as db:
            self.assertEqual(db.get(Job, interrupted).status, 'queued')
            self.assertEqual(db.get(Job, done).status, 'done')

    def test_analysis_overlaps_video_and_speech(self):
        barrier = threading.Barrier(2, timeout=5)
        def visual(*args):
            barrier.wait()
            return np.zeros(25)
        def spoken(*args):
            barrier.wait()
            return np.zeros(25), []
        source = SimpleNamespace(id='x', status='queued', prompt='', words=[], duration=25, error=None)
        with patch('worker.probe', return_value=(25, True)), patch('worker.motion_signal', side_effect=visual), patch('worker.audio_and_words', side_effect=spoken):
            worker.analyze(SimpleNamespace(commit=lambda: None,scalars=lambda query:iter([])), source)
        self.assertEqual(source.status, 'ready')

    def test_model_reused_across_recordings_and_audio_cleaned_up(self):
        worker.whisper_model.cache_clear()
        wav = Path(scratch.name)/'cache.wav'
        fake_model = SimpleNamespace(transcribe=lambda *args, **kwargs: (iter([]), None))
        def fake_audio(*args):
            with wave.open(str(wav),'wb') as out:
                out.setparams((1,2,16000,0,'NONE','not compressed'))
                out.writeframes(b'\x00\x00'*16000)
            return np.zeros(25), wav
        try:
            with patch('faster_whisper.WhisperModel', return_value=fake_model) as constructor, patch('worker.audio_signal', side_effect=fake_audio):
                worker.audio_and_words(Path(scratch.name)/'cache.mp4', 25, True)
                worker.audio_and_words(Path(scratch.name)/'cache.mp4', 25, True)
                self.assertEqual(constructor.call_count, 1)
                self.assertFalse(wav.exists())
        finally:
            worker.whisper_model.cache_clear()

    def test_fresh_pass_reuses_transcript_including_silent_recordings(self):
        wav=Path(scratch.name)/'cached.wav'
        for cached in ([],[{'start':0,'end':1,'text':'hello'}]):
            with patch('worker.audio_signal',return_value=(np.zeros(25),wav)),patch('worker.whisper_model') as model:
                _,words=worker.audio_and_words(Path(scratch.name)/'cached.mp4',25,True,cached)
                self.assertEqual(words,cached)
                model.assert_not_called()

    def test_shutdown_drains_active_jobs(self):
        self.job('preview')
        stop = threading.Event()
        completed = threading.Event()
        def fake_process(db, job):
            stop.set()
            completed.set()
        with patch('worker.process', side_effect=fake_process):
            worker.serve(stop, 1)
        self.assertTrue(completed.is_set())
        with Session() as db:
            self.assertEqual(db.get(Job, self.ids[0]).status, 'done')
