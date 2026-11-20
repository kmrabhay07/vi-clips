import unittest
import shutil
import uuid
import json
from pathlib import Path
from unittest.mock import patch
from support import scratch
from fastapi.testclient import TestClient
from sqlalchemy import select,delete
from db import DATA, Session, Job, Source, Clip, SourceProgress, init
from main import app
from worker import process,run_job,ClaimedJob
from youtube import video_id,download_video,prepare_video
from media import run,probe

class YouTubeTests(unittest.TestCase):
    def setUp(self):
        init()
        self.ids=[]
        self.clip_ids=[]

    def tearDown(self):
        with Session.begin() as db:
            for id in self.clip_ids:
                db.execute(delete(Job).where(Job.target==id))
                db.execute(delete(Clip).where(Clip.id==id))
            for id in self.ids:
                db.execute(delete(Job).where(Job.target==id))
                db.execute(delete(SourceProgress).where(SourceProgress.source_id==id))
                db.execute(delete(Source).where(Source.id==id))
                (DATA/f'{id}.mp4').unlink(missing_ok=True)

    def test_accepts_watch_share_shorts_embed_live_urls(self):
        for url in ['https://youtu.be/abcdefghijk?si=example','https://www.youtube.com/watch?v=abcdefghijk&t=2','https://m.youtube.com/shorts/abcdefghijk','https://youtube.com/embed/abcdefghijk','https://youtube.com/live/abcdefghijk']:
            self.assertEqual(video_id(url),'abcdefghijk')

    def test_rejects_non_youtube_and_malformed_urls(self):
        for url in ['http://127.0.0.1/watch?v=abcdefghijk','https://youtube.com.evil.test/watch?v=abcdefghijk','file:///etc/passwd','https://www.youtube.com/playlist?list=abc','https://youtu.be/short','https://user:password@youtube.com/watch?v=abcdefghijk']:
            with self.assertRaises(ValueError):
                video_id(url)

    def test_accepts_reported_link_plain_or_wrapped(self):
        url='https://youtu.be/AbCdefghI01?si=GULZar4yEFnOs_j0'
        for value in [url,f'[{url}]({url})',f'[Watch video]({url})',f'<{url}>',f'`{url}`','youtu.be/AbCdefghI01',f'\u200b{url}\u200b']:
            self.assertEqual(video_id(value),'AbCdefghI01')

    def test_download_normalizes_media_and_cleans_temporary_files(self):
        fixture=Path(scratch.name)/'youtube-fixture.mp4'
        target=Path(scratch.name)/'youtube-imported.mp4'
        run(['ffmpeg','-y','-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=5','-t','21','-c:v','libx264','-threads','1',str(fixture)])
        class DownloadFixture:
            def __init__(self,options):
                self.options=options
            def __enter__(self):
                return self
            def __exit__(self,*args):
                pass
            def extract_info(self,url,download):
                self.url=url
                output=Path(self.options['outtmpl'].replace('%(ext)s','mp4'))
                shutil.copyfile(fixture,output)
                return {'title':'Downloaded fixture','duration':21}
        try:
            updates=[]
            with patch('yt_dlp.YoutubeDL',DownloadFixture),patch('youtube.run',wraps=run) as commands:
                title,duration=download_video('ABCdefghi03',target,on_update=updates.append)
            self.assertFalse(any(c.args[0][0]=='ffmpeg' for c in commands.call_args_list), 'Compatible MP4 must not be copied through FFmpeg again')
            self.assertEqual(updates[-1]['stage'],'preparing')
            self.assertEqual(title,'Downloaded fixture')
            self.assertAlmostEqual(duration,21,delta=.1)
            self.assertAlmostEqual(probe(target)[0],21,delta=.1)
            self.assertFalse(list(Path(scratch.name).glob('youtube-*/*')))
        finally:
            fixture.unlink(missing_ok=True)
            target.unlink(missing_ok=True)

    def test_cached_download_is_reused_after_preparation_failure(self):
        id='ABCdefghi06'
        folder=Path(scratch.name)/'youtube-cache'/id
        folder.mkdir(parents=True,exist_ok=True)
        source=folder/'source.mkv';target=Path(scratch.name)/'cached-import.mp4'
        run(['ffmpeg','-y','-v','error','-f','lavfi','-i','color=black:size=160x90:rate=1','-t','21','-c:v','libx264','-threads','1',str(source)])
        (folder/'metadata.json').write_text(json.dumps({'title':'Preserved download'}))
        try:
            with patch('yt_dlp.YoutubeDL',side_effect=AssertionError('Must not redownload')):
                with patch('youtube.prepare_video',side_effect=RuntimeError('temporary failure')):
                    with self.assertRaises(RuntimeError):
                        download_video(id,target)
                self.assertTrue(source.exists())
                title,_=download_video(id,target)
            self.assertEqual(title,'Preserved download')
            self.assertTrue(target.exists())
            self.assertFalse(folder.exists())
        finally:
            target.unlink(missing_ok=True)
            if folder.exists():
                shutil.rmtree(folder)

    def test_av1_opus_download_is_remuxed_without_video_reencode(self):
        source=Path(scratch.name)/'av1-opus.mkv';target=Path(scratch.name)/'av1-aac.mp4'
        try:
            run(['ffmpeg','-y','-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=1','-f','lavfi','-i','sine=frequency=440:sample_rate=16000','-t','21','-c:v','libaom-av1','-cpu-used','8','-threads','1','-c:a','libopus',str(source)])
            prepare_video(source,target)
            info=json.loads(run(['ffprobe','-v','error','-show_entries','stream=codec_type,codec_name','-of','json',str(target)]))
            self.assertEqual({s['codec_type']:s['codec_name'] for s in info['streams']},{'video':'av1','audio':'aac'})
            self.assertAlmostEqual(probe(target)[0],21,delta=.2)
        finally:
            source.unlink(missing_ok=True);target.unlink(missing_ok=True)

    def test_link_queues_import_once_then_analysis(self):
        with TestClient(app) as client:
            response=client.post('/api/resolve',json={'url':'https://youtu.be/ABCdefghi01','prompt':'close calls'})
            self.assertEqual(response.status_code,200,response.text)
            source_id=response.json()['id'];self.ids.append(source_id)
            self.assertTrue(response.json()['importing'])
            self.assertEqual(client.post('/api/resolve',json={'url':'https://youtube.com/shorts/ABCdefghi01'}).json()['id'],source_id)
            with Session() as db:
                jobs=list(db.scalars(select(Job).where(Job.target==source_id)))
                self.assertEqual(len(jobs),1)
                self.assertEqual(jobs[0].kind,'import_youtube')
                def download(id,path,on_update=None):
                    path.write_bytes(b'fixture')
                    if on_update:
                        on_update({'stage':'preparing','title':'Imported video','duration':60})
                    return 'Imported video',60
                with patch('worker.download_video',side_effect=download):
                    process(db,jobs[0]);jobs[0].status='done';db.commit()
                self.assertEqual(db.get(Source,source_id).name,'Imported video')
                db.expire_all()
                self.assertEqual(db.get(SourceProgress,source_id).details['stage'],'downloaded')
                self.assertEqual(len(list(db.scalars(select(Job).where(Job.target==source_id,Job.kind=='analyze',Job.status=='queued')))),1)

    def test_existing_rejected_results_can_request_fresh_clips_once(self):
        with TestClient(app) as client:
            result=client.post('/api/resolve',json={'url':'https://youtu.be/ABCdefghi05'})
            source_id=result.json()['id'];self.ids.append(source_id)
            clip_id=uuid.uuid4().hex;self.clip_ids.append(clip_id)
            with Session.begin() as db:
                source=db.get(Source,source_id);source.status='ready';source.duration=120
                db.scalar(select(Job).where(Job.target==source_id)).status='done'
                db.add(Clip(id=clip_id,source_id=source_id,title='Rejected earlier',start=0,end=30,status='rejected'))
            opened=client.post('/api/resolve',json={'url':'https://youtu.be/ABCdefghi05'})
            self.assertFalse(opened.json()['importing'])
            for _ in range(2):
                fresh=client.post('/api/resolve',json={'url':'https://youtu.be/ABCdefghi05','regenerate':True,'prompt':'fresh moments'})
                self.assertEqual(fresh.status_code,200,fresh.text)
                self.assertTrue(fresh.json()['importing'])
            with Session() as db:
                self.assertEqual(len(list(db.scalars(select(Job).where(Job.target==source_id,Job.kind=='highlights',Job.status=='queued')))),1)
                self.assertEqual(db.get(Clip,clip_id).status,'rejected')
                self.assertEqual(db.get(Source,source_id).prompt,'fresh moments')

    def test_failed_import_exposes_error_and_can_retry_without_file(self):
        with TestClient(app) as client:
            response=client.post('/api/resolve',json={'url':'https://youtu.be/ABCdefghi02'})
            source_id=response.json()['id'];self.ids.append(source_id)
            with Session.begin() as db:
                job=db.scalar(select(Job).where(Job.target==source_id));job.status='running';job_id=job.id
            with patch('worker.download_video',side_effect=RuntimeError('YouTube requires sign-in')):
                with self.assertLogs('vi-clips.worker',level='ERROR'):
                    run_job(ClaimedJob(job_id,'import_youtube',source_id))
            with Session() as db:
                self.assertEqual(db.get(Source,source_id).status,'failed')
                self.assertIn('sign-in',db.get(Job,job_id).error)
            self.assertEqual(client.post(f'/api/jobs/{job_id}/retry').status_code,200)

    def test_failed_link_can_be_replaced_with_uploaded_original(self):
        fixture=Path(scratch.name)/'fallback.mp4'
        run(['ffmpeg','-y','-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=5','-t','21','-c:v','libx264','-threads','1',str(fixture)])
        try:
            with TestClient(app) as client:
                result=client.post('/api/resolve',json={'url':'https://youtu.be/ABCdefghi04'})
                source_id=result.json()['id'];self.ids.append(source_id)
                with Session.begin() as db:
                    db.get(Source,source_id).status='failed'
                    job=db.scalar(select(Job).where(Job.target==source_id));job.status='failed';job.error='Blocked'
                    job_id=job.id
                with fixture.open('rb') as file:
                    response=client.post('/api/sources',files={'file':('original.mp4',file,'video/mp4')},data={'youtube_url':'https://youtu.be/ABCdefghi04'})
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(response.json()['id'],source_id)
                self.assertTrue((DATA/f'{source_id}.mp4').exists())
                with Session() as db:
                    self.assertEqual(db.get(Job,job_id).status,'superseded')
                    self.assertIsNotNone(db.scalar(select(Job).where(Job.target==source_id,Job.kind=='analyze',Job.status=='queued')))
        finally:
            fixture.unlink(missing_ok=True)
