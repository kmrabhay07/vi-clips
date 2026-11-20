"""Real FFmpeg + API integration, no external model download or YouTube calls."""
import unittest
import zipfile
import io
from datetime import datetime,timedelta,timezone
from pathlib import Path

from support import scratch
from fastapi.testclient import TestClient
from main import app
from db import Session, Job, Clip, Source, engine
from media import run, probe
from worker import process
from sqlalchemy import select

class WorkflowTests(unittest.TestCase):
    def test_upload_review_export_schedule(self):
        fixture = Path(scratch.name)/'fixture.mp4'
        run(['ffmpeg','-y','-v','error','-f','lavfi','-i','testsrc2=size=320x180:rate=12','-t','21','-c:v','libx264','-pix_fmt','yuv420p',str(fixture)])
        with TestClient(app) as client:
            with fixture.open('rb') as file:
                response = client.post('/api/sources', files={'file':('gameplay.mp4',file,'video/mp4')}, data={'prompt':'chases'})
            self.assertEqual(response.status_code,200,response.text)
            source_id = response.json()['id']
            # Retry the stored original without another upload, once only.
            with Session.begin() as db:
                job = db.scalar(select(Job).where(Job.target == source_id))
                job.status, job.error = 'failed', 'decoder mismatch'
                source = db.get(Source, source_id)
                source.status, source.error = 'failed', 'decoder mismatch'
                job_id = job.id
            self.assertEqual(client.post(f'/api/jobs/{job_id}/retry').status_code, 200)
            self.assertEqual(client.post(f'/api/jobs/{job_id}/retry').status_code, 409)
            # Execute persisted jobs synchronously for deterministic test progress.
            def drain():
                with Session() as db:
                    while job := db.scalar(select(Job).where(Job.status == 'queued').order_by(Job.created)):
                        process(db,job)
                        job.status='done'
                        db.commit()
            drain()
            body=dict(title='A close call', start=0, end=20, framing='fit', captions=[dict(start=0,end=2,text='That was close!')])
            response=client.post(f'/api/sources/{source_id}/clips', json=body)
            self.assertEqual(response.status_code,200,response.text)
            clip_id=response.json()['id']
            self.assertEqual(client.post('/api/export',json={'ids':[clip_id]}).status_code,409)
            drain()
            self.assertEqual(client.post(f'/api/clips/{clip_id}/decision',json={'approved':True}).status_code,200)
            plan={'ids':[clip_id],'starts_at':(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),'interval_hours':24}
            scheduled=client.post('/api/schedule',json=plan)
            self.assertEqual(scheduled.status_code,200,scheduled.text)
            self.assertEqual(scheduled.json()['rendering'],1)
            response=client.post('/api/export',json={'ids':[clip_id]})
            self.assertEqual(response.status_code,200,response.text)
            drain()
            with Session() as db:
                clip=db.get(Clip,clip_id)
                output=Path(scratch.name)/clip.export
                self.assertAlmostEqual(probe(output)[0],20,delta=.2)
                import json
                info=json.loads(run(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=width,height','-of','json',str(output)]))
                self.assertEqual(info['streams'][0],{'width':1080,'height':1920})
            response=client.post('/api/schedule',json=plan)
            self.assertEqual(response.status_code,200,response.text)
            # A queued publication cannot be invalidated by editing.
            self.assertEqual(client.put(f'/api/clips/{clip_id}',json=body).status_code,409)
            response=client.post('/api/bundle',json={'ids':[clip_id]})
            self.assertEqual(response.status_code,200,response.text)
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                self.assertIn(f'{clip_id}.mp4',archive.namelist())
                self.assertIn('studio_export',archive.read('youtube-studio-schedule.csv').decode())
            with Session.begin() as db:
                db.get(Source,source_id).youtube_id = 'abcdefghijk'
            resolved = client.post('/api/resolve',json={'url':'https://youtube.com/shorts/abcdefghijk'})
            self.assertEqual(resolved.status_code,200)
            self.assertEqual(resolved.json()['id'],source_id)
            self.assertEqual(client.get('/api/media/../../etc/passwd').status_code,404)
            # Changing format invalidates approval/export and renders a matching preview.
            self.assertEqual(client.delete(f'/api/schedule/{clip_id}').status_code,200)
            landscape = {**body, 'framing':'landscape'}
            self.assertEqual(client.put(f'/api/clips/{clip_id}',json=landscape).status_code,200)
            drain()
            with Session() as db:
                clip = db.get(Clip,clip_id)
                self.assertEqual(clip.status,'review')
                self.assertIsNone(clip.export)
                path = Path(scratch.name)/clip.preview
                info=json.loads(run(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=width,height','-of','json',str(path)]))
                self.assertEqual(info['streams'][0],{'width':640,'height':360})
            self.assertEqual(client.post(f'/api/clips/{clip_id}/decision',json={'approved':True}).status_code,200)
            self.assertEqual(client.post('/api/export',json={'ids':[clip_id]}).status_code,200)
            drain()
            with Session() as db:
                path = Path(scratch.name)/db.get(Clip,clip_id).export
                info=json.loads(run(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=width,height','-of','json',str(path)]))
                self.assertEqual(info['streams'][0],{'width':1920,'height':1080})
            self.assertEqual(client.put(f'/api/clips/{clip_id}',json={**body,'framing':'invalid'}).status_code,422)
            self.assertEqual(client.post('/api/formats',json={'ids':[clip_id],'framing':'invalid'}).status_code,422)
            response = client.post('/api/formats',json={'ids':[clip_id],'framing':'crop'})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['changed'],1)
            # A duplicate format action is a no-op, including while its preview runs.
            self.assertEqual(client.post('/api/formats',json={'ids':[clip_id],'framing':'crop'}).json()['changed'],0)
            self.assertEqual(client.post('/api/formats',json={'ids':[clip_id],'framing':'fit'}).status_code,409)
            drain()
            with Session() as db:
                clip = db.get(Clip,clip_id)
                self.assertEqual(clip.framing,'crop')
                self.assertEqual(clip.status,'review')
                self.assertIsNone(clip.export)
                path = Path(scratch.name)/clip.preview
                info=json.loads(run(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=width,height','-of','json',str(path)]))
                self.assertEqual(info['streams'][0],{'width':360,'height':640})

