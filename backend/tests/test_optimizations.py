import unittest
import uuid
import json
from urllib.parse import quote
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from sqlalchemy import event,delete,select
from fastapi.testclient import TestClient
from support import scratch
from db import DATA,Session,Source,Job,engine,init
from main import app
from storage import ensure_space,StorageFull,RESERVE
from analysis_cache import saved_signals,cached_signals
from worker import analyze
from youtube import prepare_video,download_video

class OptimizationTests(unittest.TestCase):
    def setUp(self):
        init();self.id=uuid.uuid4().hex
        self.path=DATA/f'{self.id}.mp4'

    def tearDown(self):
        with Session.begin() as db:
            db.execute(delete(Job).where(Job.target==self.id))
            db.execute(delete(Source).where(Source.id==self.id))
        self.path.unlink(missing_ok=True)

    def test_state_omits_large_columns_in_both_sql_and_response(self):
        with Session.begin() as db:
            db.add(Source(id=self.id,name='Lightweight',words=[{'start':0,'end':1,'text':'TRANSCRIPT_SENTINEL'}]*1000,
                          analysis_cache={'motion':[1]*1000}))
        sql=[]
        def capture(conn,cursor,statement,parameters,context,executemany):
            sql.append(statement)
        event.listen(engine,'before_cursor_execute',capture)
        try:
            with TestClient(app) as client:
                # Startup migration reflection is excluded from the measured state call.
                sql.clear()
                response=client.get('/api/state')
                self.assertEqual(response.status_code,200)
                self.assertNotIn('TRANSCRIPT_SENTINEL',response.text)
                source=next(s for s in response.json()['sources'] if s['id']==self.id)
                self.assertEqual(source['name'],'Lightweight')
                self.assertNotIn('words',source);self.assertNotIn('analysis_cache',source)
                queries=[q for q in sql if 'FROM sources' in q]
                self.assertEqual(len(queries),1)
                self.assertNotIn('sources.words',queries[0])
                self.assertNotIn('sources.analysis_cache',queries[0])
        finally:
            event.remove(engine,'before_cursor_execute',capture)

    def test_analysis_cache_rejects_changed_file_and_invalid_data(self):
        self.path.write_bytes(b'recording')
        motion=np.arange(25,dtype=float);audio=np.zeros(25)
        data=saved_signals(self.path,25,motion,audio)
        np.testing.assert_array_equal(cached_signals(data,self.path,25)[0],motion)
        self.assertIsNone(cached_signals({**data,'audio':[float('nan')]*25},self.path,25))
        self.assertIsNone(cached_signals(data,self.path,26))
        self.path.write_bytes(b'changed recording')
        self.assertIsNone(cached_signals(data,self.path,25))

    def test_regenerate_uses_cache_without_decoding_or_transcribing(self):
        self.path.write_bytes(b'recording')
        data=saved_signals(self.path,25,np.zeros(25),np.zeros(25))
        with Session() as db:
            source=Source(id=self.id,name='Cached',duration=25,analysis_cache=data,words=[])
            db.add(source);db.commit()
            with patch('worker.probe',return_value=(25,False)),patch('worker.motion_signal') as motion,patch('worker.audio_and_words') as audio,patch('worker.choose',return_value=[]):
                analyze(db,source,regenerate=True)
                motion.assert_not_called();audio.assert_not_called()
            self.assertEqual(source.status,'ready');db.commit()

    def test_space_guard_keeps_reserve_and_blocks_remux_before_ffmpeg(self):
        with patch('storage.shutil.disk_usage',return_value=SimpleNamespace(free=RESERVE+10)):
            ensure_space(DATA,10)
            with self.assertRaises(StorageFull):ensure_space(DATA,11)
        self.path.write_bytes(b'recording')
        with patch('youtube.ensure_space',side_effect=StorageFull('Not enough working space')),patch('youtube.run') as command:
            with self.assertRaises(StorageFull):prepare_video(self.path,DATA/'out.mp4')
            command.assert_not_called()
        self.assertTrue(self.path.exists())

    def test_low_space_upload_returns_507_without_creating_source(self):
        with TestClient(app) as client,patch('main.ensure_space',side_effect=StorageFull('Not enough working space')):
            response=client.post('/api/sources',files={'file':('test.mp4',b'video','video/mp4')})
            self.assertEqual(response.status_code,507)
            self.assertEqual(response.json()['detail'],'Not enough working space')

    def test_raw_upload_streams_file_and_preserves_webcam_setup(self):
        content=b'video bytes'
        metadata=dict(filename='recording.mp4',file_size=len(content),prompt='close calls',layout_setup=True)
        with TestClient(app) as client,patch('main.probe',return_value=(25,False)):
            response=client.post('/api/sources/raw',content=content,headers={'x-vi-upload':quote(json.dumps(metadata)),'content-type':'video/mp4'})
            self.assertEqual(response.status_code,200,response.text)
            self.id=response.json()['id'];self.path=DATA/f'{self.id}.mp4'
            self.assertEqual(self.path.read_bytes(),content)
            with Session() as db:
                self.assertEqual(db.get(Source,self.id).status,'awaiting_layout')
                self.assertIsNone(db.scalar(select(Job).where(Job.target==self.id)))
            media=client.get(f'/api/media/{self.id}.mp4',headers={'Range':'bytes=0-4'})
            self.assertEqual(media.status_code,206)
            self.assertEqual(media.content,content[:5])

    def test_raw_upload_rejects_incomplete_file(self):
        metadata=dict(filename='recording.mp4',file_size=100)
        with TestClient(app) as client:
            response=client.post('/api/sources/raw',content=b'short',headers={'x-vi-upload':quote(json.dumps(metadata))})
            self.assertEqual(response.status_code,400)
            self.assertIn('incomplete',response.json()['detail'])

    def test_space_checks_credit_download_components_when_resuming(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            root=Path(directory);folder=root/'youtube-cache'/'resumeTest1';folder.mkdir(parents=True)
            (folder/'source.f299.mp4').write_bytes(b'x'*40)
            (folder/'source.temp.mp4').write_bytes(b'x'*10)
            def downloader(options):
                options['match_filter']({'duration':25,'requested_formats':[{'filesize':60},{'filesize':40}]})
                options['postprocessor_hooks'][0]({'status':'started','postprocessor':'Merger'})
                raise RuntimeError('End mocked network test')
            with patch('yt_dlp.YoutubeDL',side_effect=downloader),patch('youtube.ensure_space') as room:
                with self.assertRaises(RuntimeError):download_video('resumeTest1',root/'out.mp4')
            self.assertEqual([call.args[1] for call in room.call_args_list],[150,30])
            self.assertTrue((folder/'source.f299.mp4').exists())
