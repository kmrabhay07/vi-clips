import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from fastapi.testclient import TestClient
from support import scratch
from sqlalchemy import delete
from db import Session,DATA,Source,Clip,Job,Publication,YouTubeUpload,CleanupPolicy,PublishedHistory,init
from published_cleanup import cleanup_published,remove_unused_sources
from main import app


class PublishedCleanupTests(unittest.TestCase):
    def setUp(self):
        init();self.source=uuid.uuid4().hex;self.id=uuid.uuid4().hex;self.other=uuid.uuid4().hex
        with Session.begin() as db:
            db.add(Source(id=self.source,name='Original',duration=60,status='ready'));db.flush()
            db.add(Clip(id=self.id,source_id=self.source,title='Published clip',start=0,end=25,status='exported',export=self.id+'-export.mp4'));db.flush()
            db.add(Publication(clip_id=self.id,scheduled_at=datetime.now(timezone.utc)-timedelta(hours=1),status='scheduled'))
            db.add(YouTubeUpload(clip_id=self.id,channel_id='test',youtube_id='AbCdefghI01',status='scheduled'))
        self.original=DATA/(self.source+'.mp4');self.output=DATA/(self.id+'-export.mp4')
        self.original.write_bytes(b'original');self.output.write_bytes(b'clip')

    def tearDown(self):
        with Session.begin() as db:
            for id in [self.id,self.other]:
                for model,column in [(Job,Job.target),(PublishedHistory,PublishedHistory.clip_id),(YouTubeUpload,YouTubeUpload.clip_id),(Publication,Publication.clip_id),(Clip,Clip.id)]:
                    db.execute(delete(model).where(column==id))
            db.execute(delete(Job).where(Job.target==self.source));db.execute(delete(Source).where(Source.id==self.source));db.execute(delete(CleanupPolicy))
        self.original.unlink(missing_ok=True);self.output.unlink(missing_ok=True)

    def enable(self,sources=False):
        with Session.begin() as db:
            db.add(CleanupPolicy(id=1,clips=True,sources=sources))

    def public(self,id):
        with Session.begin() as db:
            item=db.get(YouTubeUpload,id);item.status='published';item.remote={'privacyStatus':'public','uploadStatus':'processed'}

    def test_disabled_and_failed_verification_never_delete(self):
        with patch('youtube_publish.refresh_remote') as refresh:
            cleanup_published();refresh.assert_not_called()
        self.enable(True)
        with patch('youtube_publish.refresh_remote',side_effect=ValueError('offline')):
            cleanup_published()
        self.assertTrue(self.original.exists());self.assertTrue(self.output.exists())

    def test_scheduled_private_and_unprocessed_are_preserved(self):
        self.enable(True)
        with patch('youtube_publish.refresh_remote'):
            cleanup_published()
        self.assertTrue(self.output.exists())
        with Session.begin() as db:
            item=db.get(YouTubeUpload,self.id);item.status='published';item.remote={'privacyStatus':'public','uploadStatus':'uploaded'}
        with patch('youtube_publish.refresh_remote'):
            cleanup_published()
        self.assertTrue(self.output.exists())

    def test_clip_cleaned_history_kept_source_waits_for_remaining_clip(self):
        self.enable(True)
        with Session.begin() as db:
            db.add(Clip(id=self.other,source_id=self.source,title='Still reviewing',start=25,end=50,status='review'))
        with patch('youtube_publish.refresh_remote',side_effect=self.public):
            result=cleanup_published()
        self.assertEqual(result['clips_deleted'],1);self.assertFalse(self.output.exists());self.assertTrue(self.original.exists())
        with Session.begin() as db:
            self.assertIsNone(db.get(Clip,self.id));self.assertIsNone(db.get(YouTubeUpload,self.id));self.assertIsNone(db.get(Publication,self.id))
            self.assertEqual(db.get(PublishedHistory,self.id).youtube_id,'AbCdefghI01')
            db.delete(db.get(Clip,self.other))
            db.add(Job(id=self.other,kind='highlights',target=self.source,status='queued'))
        self.assertEqual(remove_unused_sources(),0);self.assertTrue(self.original.exists())
        with Session.begin() as db:
            db.get(Job,self.other).status='done'
        self.assertEqual(remove_unused_sources(),1);self.assertFalse(self.original.exists())
        with Session() as db:
            self.assertIsNotNone(db.get(PublishedHistory,self.id))

    def test_active_render_blocks_clip_cleanup(self):
        self.enable(True)
        with Session.begin() as db:
            db.add(Job(id=uuid.uuid4().hex,kind='export',target=self.id,status='running'))
        with patch('youtube_publish.refresh_remote',side_effect=self.public):
            result=cleanup_published()
        self.assertEqual(result['clips_deleted'],0);self.assertTrue(self.output.exists())

    def test_policy_api_defaults_validation_and_origin(self):
        with TestClient(app) as client:
            self.assertEqual(client.get('/api/state').json()['cleanup_policy'],{'clips':False,'sources':False})
            self.assertEqual(client.put('/api/cleanup-policy',json={'clips':False,'sources':True}).status_code,422)
            self.assertEqual(client.put('/api/cleanup-policy',json={'clips':True},headers={'Origin':'https://other.example'}).status_code,403)
            self.assertEqual(client.put('/api/cleanup-policy',json={'clips':True,'sources':True}).status_code,200)
            self.assertTrue(self.output.exists())
