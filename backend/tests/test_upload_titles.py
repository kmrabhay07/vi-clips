import unittest
import uuid
from datetime import datetime, timedelta, timezone
from support import scratch
from fastapi.testclient import TestClient
from db import DATA, Session, Source, Clip, Publication, YouTubeUpload, Job, init
from main import app
from sqlalchemy import delete, select


class UploadTitleTests(unittest.TestCase):
    def setUp(self):
        init()
        self.source,self.id=[uuid.uuid4().hex for _ in range(2)]
        self.path=DATA/(self.id+'-export.mp4')
        self.path.write_bytes(b'existing rendered media')
        with Session.begin() as db:
            db.add(Source(id=self.source,name='Title test',duration=60,status='ready'))
            db.flush()
            db.add(Clip(id=self.id,source_id=self.source,title='Old title',start=0,end=25,status='exported',preview='preview.mp4',export=self.path.name))
        self.client=TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None,None,None)
        with Session.begin() as db:
            for model,column in [(YouTubeUpload,YouTubeUpload.clip_id),(Publication,Publication.clip_id),(Job,Job.target),(Clip,Clip.id)]:
                db.execute(delete(model).where(column==self.id))
            db.execute(delete(Source).where(Source.id==self.source))
        self.path.unlink(missing_ok=True)

    def test_schedule_saves_title_and_queue_rename_preserves_export(self):
        response=self.client.post('/api/schedule',json={'ids':[self.id],'starts_at':(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),'titles':{self.id:'Upload title'}})
        self.assertEqual(response.status_code,200,response.text)
        response=self.client.put(f'/api/clips/{self.id}/title',json={'title':'  Better title  '})
        self.assertEqual(response.status_code,200,response.text)
        with Session() as db:
            clip=db.get(Clip,self.id)
            self.assertEqual(clip.title,'Better title')
            self.assertEqual(clip.export,self.path.name)
            self.assertEqual(clip.status,'exported')
            self.assertIsNone(db.scalar(select(Job).where(Job.target==self.id)))
        self.assertTrue(self.path.exists())

    def test_started_upload_rejects_rename_and_titles_validate(self):
        for title in ('   ','bad<title>','x'*101):
            self.assertEqual(self.client.put(f'/api/clips/{self.id}/title',json={'title':title}).status_code,422)
        with Session.begin() as db:
            db.add(YouTubeUpload(clip_id=self.id,channel_id='test',status='uploading'))
        self.assertEqual(self.client.put(f'/api/clips/{self.id}/title',json={'title':'New'}).status_code,409)
        with Session.begin() as db:
            item=db.get(YouTubeUpload,self.id)
            item.status='retry'
            item.session_cipher='saved-session'
        self.assertEqual(self.client.put(f'/api/clips/{self.id}/title',json={'title':'New'}).status_code,409)
