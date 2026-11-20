import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from fastapi.testclient import TestClient
from sqlalchemy import select, delete
from support import scratch
from db import DATA, Session, Source, Clip, Job, init
from main import app
from media import render, run
from worker import process, analyze

LAYOUT = dict(face=dict(x=0,y=0,width=.5,height=1), gameplay=dict(x=.5,y=0,width=.5,height=1),top_fraction=.3,face_on_top=True)

class DualCropTests(unittest.TestCase):
    def setUp(self):
        init()
        self.id=uuid.uuid4().hex

    def tearDown(self):
        with Session.begin() as db:
            ids=list(db.scalars(select(Clip.id).where(Clip.source_id==self.id)))
            db.execute(delete(Job).where(Job.target.in_([self.id,*ids])))
            db.execute(delete(Clip).where(Clip.source_id==self.id))
            db.execute(delete(Source).where(Source.id==self.id))
        (DATA/f'{self.id}.mp4').unlink(missing_ok=True)

    def test_upload_waits_for_layout_and_save_queues_analysis_once(self):
        fixture=Path(scratch.name)/'dual-upload.mp4'
        fixture.write_bytes(b'fixture')
        with TestClient(app) as client, patch('main.probe',return_value=(21,False)):
            response=client.post('/api/sources',files={'file':('dual.mp4',fixture.read_bytes(),'video/mp4')},data={'layout_setup':'true'})
            self.assertEqual(response.status_code,200,response.text)
            self.id=response.json()['id']
            with Session() as db:
                self.assertEqual(db.get(Source,self.id).status,'awaiting_layout')
                self.assertIsNone(db.scalar(select(Job).where(Job.target==self.id)))
            invalid={**LAYOUT,'face':dict(x=.9,y=0,width=.5,height=1)}
            self.assertEqual(client.put(f'/api/sources/{self.id}/layout',json={'dual_crop':invalid}).status_code,422)
            result=client.put(f'/api/sources/{self.id}/layout',json={'dual_crop':LAYOUT})
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(client.put(f'/api/sources/{self.id}/layout',json={'dual_crop':LAYOUT}).status_code,409)
            with Session() as db:
                source=db.get(Source,self.id)
                self.assertFalse(source.layout_pending)
                self.assertEqual(source.dual_crop,LAYOUT)
                self.assertEqual(len(list(db.scalars(select(Job).where(Job.target==self.id)))),1)

    def test_link_download_pauses_before_analysis(self):
        with Session() as db:
            source=Source(id=self.id,name='Dual link',youtube_id='dualTest001',layout_pending=True)
            db.add(source);db.commit()
            def downloaded(*args,**kwargs):
                (DATA/f'{self.id}.mp4').write_bytes(b'fixture')
                return 'Imported recording',25
            with patch('worker.download_video',side_effect=downloaded):
                process(db,SimpleNamespace(kind='import_youtube',target=self.id))
            self.assertEqual(source.status,'awaiting_layout')
            self.assertIsNone(db.scalar(select(Job).where(Job.target==self.id)))
            db.commit()

    def test_generated_clips_inherit_layout(self):
        with Session() as db:
            source=Source(id=self.id,name='Stacked',dual_crop=LAYOUT)
            db.add(source);db.commit()
            with patch('worker.probe',return_value=(25,False)),patch('worker.motion_signal',return_value=np.zeros(25)),patch('worker.audio_and_words',return_value=(np.zeros(25),[])),patch('worker.choose',return_value=[dict(start=0,end=25,score=80,reason='Test')]):
                analyze(db,source)
            clip=db.scalar(select(Clip).where(Clip.source_id==self.id))
            self.assertEqual(clip.framing,'dual')
            self.assertEqual(clip.dual_crop,LAYOUT)
            db.commit()

    def test_import_frame_returns_one_jpeg_without_queueing_analysis(self):
        with Session.begin() as db:
            db.add(Source(id=self.id,name='Webcam setup',duration=21,status='awaiting_layout',layout_pending=True))
        with TestClient(app) as client:
            self.assertEqual(client.get('/api/sources/missing/layout-frame').status_code,404)
            self.assertEqual(client.get(f'/api/sources/{self.id}/layout-frame').status_code,409)
            path=DATA/f'{self.id}.mp4'
            run(['ffmpeg','-y','-v','error','-f','lavfi','-i','color=blue:s=320x180:r=1:d=21','-c:v','libx264','-threads','1',str(path)])
            response=client.get(f'/api/sources/{self.id}/layout-frame')
            self.assertEqual(response.status_code,200,response.text[:100] if response.status_code!=200 else '')
            self.assertEqual(response.headers['content-type'],'image/jpeg')
            self.assertTrue(response.content.startswith(b'\xff\xd8'))
            self.assertTrue(response.content.endswith(b'\xff\xd9'))
        with Session() as db:
            self.assertEqual(db.get(Source,self.id).status,'awaiting_layout')
            self.assertIsNone(db.scalar(select(Job).where(Job.target==self.id)))

    def test_real_render_stacks_correct_regions_in_preview_and_1080p(self):
        source=Path(scratch.name)/'dual-colors.mp4'
        run(['ffmpeg','-y','-v','error','-f','lavfi','-i','color=red:s=320x180:d=1','-vf','drawbox=x=160:y=0:w=160:h=180:color=blue:t=fill','-c:v','libx264','-threads','1',str(source)])
        for full,face_on_top in [(False,True),(True,False)]:
            output=Path(scratch.name)/f'dual-{full}.mp4'
            clip=SimpleNamespace(framing='dual',dual_crop={**LAYOUT,'face_on_top':face_on_top},start=0,end=.2,captions=[],caption_style='none')
            render(source,clip,output,full=full)
            width,height=(1080,1920) if full else (360,640)
            raw=run(['ffmpeg','-v','error','-i',str(output),'-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','-'])
            frame=np.frombuffer(raw,dtype=np.uint8).reshape(height,width,3)
            top=frame[int(height*.1),width//2];bottom=frame[int(height*.7),width//2]
            self.assertGreater(int(top[0] if face_on_top else top[2]),200)
            self.assertGreater(int(bottom[2] if face_on_top else bottom[0]),200)
