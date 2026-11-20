import unittest
import uuid
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from support import scratch
from sqlalchemy import select,delete
from fastapi.testclient import TestClient
from db import DATA,Session,Source,Clip,Job,Publication,init
from main import app
from worker import process,run_job,ClaimedJob

class PublishingTests(unittest.TestCase):
    def setUp(self):
        init()
        self.source_id=uuid.uuid4().hex
        self.clip_ids=[uuid.uuid4().hex for _ in range(2)]
        with Session.begin() as db:
            db.add(Source(id=self.source_id,name='Publishing fixture',duration=90,status='ready'))
            db.flush()
            for index,id in enumerate(self.clip_ids):
                db.add(Clip(id=id,source_id=self.source_id,title=f'Approved {index}',start=index*25,end=index*25+25,status='approved',preview=f'{id}-preview.mp4'))
        self.client=TestClient(app);self.client.__enter__()
        self.plan={'ids':self.clip_ids,'starts_at':(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),'interval_hours':24}

    def tearDown(self):
        self.client.__exit__(None,None,None)
        with Session.begin() as db:
            db.execute(delete(Job).where(Job.target.in_(self.clip_ids)))
            db.execute(delete(Publication).where(Publication.clip_id.in_(self.clip_ids)))
            db.execute(delete(Clip).where(Clip.id.in_(self.clip_ids)))
            db.execute(delete(Source).where(Source.id==self.source_id))
        for id in self.clip_ids:
            for suffix in ('-preview.mp4','-export.mp4','-export.ass'):
                (DATA/f'{id}{suffix}').unlink(missing_ok=True)

    def test_schedule_approved_clips_automatically_renders_once(self):
        response=self.client.post('/api/schedule',json=self.plan)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['rendering'],2)
        self.assertEqual(self.client.post('/api/schedule',json=self.plan).status_code,200)
        self.assertEqual(self.client.post('/api/export',json={'ids':self.clip_ids}).status_code,200)
        with Session() as db:
            jobs=list(db.scalars(select(Job).where(Job.target.in_(self.clip_ids),Job.kind=='export')))
            self.assertEqual(len(jobs),2)
            self.assertTrue(all(db.get(Publication,id).status=='rendering' for id in self.clip_ids))
            def rendered(source,clip,output,full):
                output.write_bytes(b'completed fixture export')
            with patch('worker.render',side_effect=rendered):
                for job in jobs:
                    process(db,job);job.status='done';db.commit()
            self.assertTrue(all(db.get(Publication,id).status=='studio_export' for id in self.clip_ids))
        self.assertEqual(self.client.post('/api/bundle',json={'ids':self.clip_ids}).status_code,200)
        queued=self.client.get('/api/state').json()['publications']
        times=[item['scheduled_at'] for item in queued if item['clip_id'] in self.clip_ids]
        self.assertEqual(times,sorted(times))

    def test_review_clips_rejected_without_partial_queue_or_jobs(self):
        with Session.begin() as db:
            db.get(Clip,self.clip_ids[1]).status='review'
        self.assertEqual(self.client.post('/api/schedule',json=self.plan).status_code,409)
        with Session() as db:
            self.assertFalse(list(db.scalars(select(Publication).where(Publication.clip_id.in_(self.clip_ids)))))
            self.assertFalse(list(db.scalars(select(Job).where(Job.target.in_(self.clip_ids)))))

    def test_render_failure_is_reported_and_retry_restores_queue(self):
        self.assertEqual(self.client.post('/api/schedule',json=self.plan).status_code,200)
        with Session() as db:
            job=db.scalar(select(Job).where(Job.target==self.clip_ids[0],Job.kind=='export'))
            claim=ClaimedJob(job.id,job.kind,job.target)
        with patch('worker.render',side_effect=RuntimeError('encoder failed')),self.assertLogs('vi-clips.worker',level='ERROR'):
            run_job(claim)
        with Session() as db:
            self.assertEqual(db.get(Publication,self.clip_ids[0]).status,'render_failed')
            self.assertEqual(db.get(Clip,self.clip_ids[0]).status,'approved')
        self.assertEqual(self.client.post(f'/api/jobs/{claim.id}/retry').status_code,200)
        with Session() as db:
            self.assertEqual(db.get(Publication,self.clip_ids[0]).status,'rendering')
            self.assertEqual(db.get(Job,claim.id).status,'queued')

    def test_missing_export_is_repaired_and_changed_schedule_conflicts(self):
        with Session.begin() as db:
            clip=db.get(Clip,self.clip_ids[0]);clip.status='exported';clip.export=f'{clip.id}-export.mp4'
        response=self.client.post('/api/schedule',json=self.plan)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['rendering'],2)
        changed={**self.plan,'interval_hours':12}
        self.assertEqual(self.client.post('/api/schedule',json=changed).status_code,409)
