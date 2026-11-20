import unittest
import uuid
from datetime import datetime, timezone
from support import scratch
from fastapi.testclient import TestClient
from db import DATA, Session, Source, Clip, Job, Publication, Rejection, SourceProgress, init
from main import app


class SourceDeletionTests(unittest.TestCase):
    def test_delete_preserves_busy_and_queued_then_removes_owned_files(self):
        init()
        source, clip, job = [uuid.uuid4().hex for _ in range(3)]
        with Session.begin() as db:
            db.add(Source(id=source,name='Delete fixture',youtube_id='delTest1234',duration=60,status='failed'))
            db.flush()
            db.add(Clip(id=clip,source_id=source,title='Clip',start=0,end=25,preview=clip+'-preview.mp4'))
            db.add(SourceProgress(source_id=source,details={}))
            db.flush()
            db.add(Rejection(clip_id=clip,delete_after=datetime.now(timezone.utc)))
            db.add(Job(id=job,kind='preview',target=clip,status='running'))
        owned=[DATA/(source+'.mp4'),DATA/(clip+'-preview.mp4'),DATA/(clip+'-export.ass')]
        unrelated=DATA/('unrelated-'+uuid.uuid4().hex+'.mp4')
        for path in [*owned,unrelated]:
            path.write_bytes(b'test')
        cache=DATA/'youtube-cache'/'delTest1234'
        cache.mkdir(parents=True)
        (cache/'source.mp4.part').write_bytes(b'partial download')
        with TestClient(app) as client:
            self.assertEqual(client.delete('/api/sources/'+source).status_code,409)
            self.assertTrue(all(p.exists() for p in owned))
            with Session.begin() as db:
                db.get(Job,job).status='failed'
                db.add(Publication(clip_id=clip,scheduled_at=datetime.now(timezone.utc)))
            self.assertEqual(client.delete('/api/sources/'+source).status_code,409)
            with Session.begin() as db:
                db.delete(db.get(Publication,clip))
            response=client.delete('/api/sources/'+source)
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['clips_deleted'],1)
            self.assertTrue(all(not p.exists() for p in owned))
            self.assertTrue(unrelated.exists())
            self.assertFalse(cache.exists())
            with Session() as db:
                for model,key in [(Source,source),(Clip,clip),(Job,job),(Rejection,clip),(SourceProgress,source)]:
                    self.assertIsNone(db.get(model,key))
            self.assertEqual(client.delete('/api/sources/'+source).status_code,404)
        unrelated.unlink()
