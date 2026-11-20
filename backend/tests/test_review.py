import unittest
import uuid
from datetime import datetime, timedelta, timezone
from support import scratch
from fastapi.testclient import TestClient
from sqlalchemy import select, delete
from db import DATA, Session, Source, Clip, Rejection, Job, Publication, init
from main import app
from retention import cleanup_rejected

class ReviewTests(unittest.TestCase):
    def setUp(self):
        init()
        self.source_id, self.clip_id = uuid.uuid4().hex, uuid.uuid4().hex
        with Session.begin() as db:
            db.add(Source(id=self.source_id,name='Original',duration=60,status='ready'))
            db.flush()
            db.add(Clip(id=self.clip_id,source_id=self.source_id,title='Clip',start=0,end=25,preview=f'{self.clip_id}-preview.mp4'))
        self.paths = [DATA/f'{self.clip_id}{suffix}' for suffix in ('-preview.mp4','-preview.ass','-export.mp4','-export.ass')]
        self.original = DATA/f'{self.source_id}.mp4'
        for path in [*self.paths,self.original]:
            path.write_bytes(b'preserve original, delete derived media only')
        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None,None,None)
        with Session.begin() as db:
            for model,column,value in [(Job,Job.target,self.clip_id),(Publication,Publication.clip_id,self.clip_id),(Rejection,Rejection.clip_id,self.clip_id),(Clip,Clip.id,self.clip_id),(Source,Source.id,self.source_id)]:
                db.execute(delete(model).where(column==value))
        for path in [*self.paths,self.original]:
            path.unlink(missing_ok=True)

    def decide(self, approved):
        response = self.client.post(f'/api/clips/{self.clip_id}/decision',json={'approved':approved})
        self.assertEqual(response.status_code,200,response.text)

    def deadline(self):
        clip = next(c for c in self.client.get('/api/state').json()['clips'] if c['id']==self.clip_id)
        return datetime.fromisoformat(clip['delete_after'])

    def test_rejected_clip_deleted_only_after_24h_original_preserved(self):
        before = datetime.now(timezone.utc)
        self.decide(False)
        deadline = self.deadline()
        self.assertTrue(23.99 <= (deadline-before).total_seconds()/3600 <= 24.01)
        self.decide(False)
        self.assertEqual(self.deadline(),deadline)
        self.assertEqual(cleanup_rejected(deadline-timedelta(seconds=1)),0)
        self.assertTrue(all(p.exists() for p in self.paths))
        self.assertEqual(cleanup_rejected(deadline+timedelta(seconds=1)),1)
        with Session() as db:
            self.assertIsNone(db.get(Clip,self.clip_id))
            self.assertIsNotNone(db.get(Source,self.source_id))
        self.assertTrue(self.original.exists())
        self.assertTrue(all(not p.exists() for p in self.paths))

    def test_approve_cancels_deletion_and_preserves_completed_export(self):
        self.decide(False)
        with Session.begin() as db:
            db.get(Clip,self.clip_id).export=f'{self.clip_id}-export.mp4'
        self.decide(True)
        self.assertEqual(cleanup_rejected(datetime.now(timezone.utc)+timedelta(days=2)),0)
        with Session() as db:
            self.assertIsNone(db.get(Rejection,self.clip_id))
            self.assertEqual(db.get(Clip,self.clip_id).status,'exported')

    def test_format_change_restores_review_and_cancels_deletion(self):
        self.decide(False)
        result=self.client.post('/api/formats',json={'ids':[self.clip_id],'framing':'landscape'})
        self.assertEqual(result.status_code,200,result.text)
        with Session() as db:
            self.assertIsNone(db.get(Rejection,self.clip_id))
            self.assertEqual(db.get(Clip,self.clip_id).status,'review')

    def test_cleanup_waits_for_active_jobs_then_deletes_job_history(self):
        self.decide(False)
        deadline=self.deadline()
        id=uuid.uuid4().hex
        with Session.begin() as db:
            db.add(Job(id=id,kind='preview',target=self.clip_id,status='running'))
        self.assertEqual(cleanup_rejected(deadline+timedelta(seconds=1)),0)
        with Session.begin() as db:
            db.get(Job,id).status='done'
        self.assertEqual(cleanup_rejected(deadline+timedelta(seconds=1)),1)
        with Session() as db:
            self.assertIsNone(db.get(Job,id))

    def test_legacy_rejection_gets_full_grace_period(self):
        with Session.begin() as db:
            db.get(Clip,self.clip_id).status='rejected'
        now=datetime.now(timezone.utc)
        self.assertEqual(cleanup_rejected(now),0)
        self.assertEqual(self.deadline(),now+timedelta(hours=24))

    def test_empty_rejected_deletes_immediately_but_keeps_original_and_approved(self):
        self.decide(False)
        approved_id=uuid.uuid4().hex
        with Session.begin() as db:
            db.add(Clip(id=approved_id,source_id=self.source_id,title='Keep me',start=25,end=50,status='approved'))
        try:
            response=self.client.delete('/api/clips/rejected')
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json(),{'deleted':1,'skipped':0})
            with Session() as db:
                self.assertIsNone(db.get(Clip,self.clip_id))
                self.assertIsNotNone(db.get(Clip,approved_id))
                self.assertIsNotNone(db.get(Source,self.source_id))
            self.assertTrue(self.original.exists())
            self.assertTrue(all(not p.exists() for p in self.paths))
            self.assertEqual(self.client.delete('/api/clips/rejected').json()['deleted'],0)
        finally:
            with Session.begin() as db:
                db.execute(delete(Clip).where(Clip.id==approved_id))
