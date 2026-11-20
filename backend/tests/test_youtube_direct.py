import json
import uuid
import hashlib
from datetime import datetime,timedelta,timezone
from urllib.parse import urlparse,parse_qs
from unittest import TestCase
from unittest.mock import patch
import httpx
from sqlalchemy import delete,select
from support import scratch
from fastapi.testclient import TestClient
from db import DATA,Session,Source,Clip,Job,Publication,YouTubeUpload,IntegrationSecret,OAuthAttempt,init
from main import app
from youtube_auth import configure_client,redirect_uri,write_secret,read_secret,begin_connection,finish_connection,SCOPES,seal,unseal,access_token
from youtube_publish import queue_upload,claim_upload,upload_one,recover_uploads,refresh_remote,CHUNK

RealClient=httpx.Client
URI='https://www.googleapis.com/upload/youtube/v3/videos?upload_id=SECRET_SESSION'
VIDEO_ID='AbCdefghI01'

class DirectYouTubeTests(TestCase):
    def setUp(self):
        init()
        self.client=TestClient(app);self.client.__enter__()
        self.source_id=uuid.uuid4().hex;self.clip_id=uuid.uuid4().hex
        self.path=DATA/f'{self.clip_id}-export.mp4';self.path.write_bytes(b'video-bytes')
        self.when=datetime.now(timezone.utc)+timedelta(days=1)
        with Session.begin() as db:
            db.add(Source(id=self.source_id,name='Uploader fixture',status='ready',duration=25));db.flush()
            db.add(Clip(id=self.clip_id,source_id=self.source_id,title='Uploader test',start=0,end=25,status='exported',export=self.path.name))
        self.document={'web':{'client_id':'fixture.apps.googleusercontent.com','client_secret':'FAKE_CLIENT_SECRET','redirect_uris':[redirect_uri()]}}

    def tearDown(self):
        self.client.__exit__(None,None,None)
        with Session.begin() as db:
            for model,column in [(Job,Job.target),(YouTubeUpload,YouTubeUpload.clip_id),(Publication,Publication.clip_id),(Clip,Clip.id)]:
                db.execute(delete(model).where(column==self.clip_id))
            db.execute(delete(Source).where(Source.id==self.source_id))
            db.execute(delete(IntegrationSecret));db.execute(delete(OAuthAttempt))
        self.path.unlink(missing_ok=True)

    def connect(self):
        configure_client(self.document)
        with Session.begin() as db:
            write_secret(db,'youtube_account',{'access_token':'FAKE_ACCESS','refresh_token':'FAKE_REFRESH',
                'expires_at':datetime.now(timezone.utc).timestamp()+3600,'channel_id':'UCfixture','channel_title':'Fixture channel'})

    def plan(self,mode='scheduled'):
        with Session.begin() as db:
            db.add(Publication(clip_id=self.clip_id,scheduled_at=self.when));db.flush()
            queue_upload(db,self.clip_id,mode)

    def transport(self,handler):
        return patch('youtube_publish.httpx.Client',side_effect=lambda **kwargs:RealClient(transport=httpx.MockTransport(handler),**kwargs))

    def completed(self):
        return {'id':VIDEO_ID,'status':{'privacyStatus':'private','publishAt':self.when.isoformat(),'uploadStatus':'uploaded'}}

    def test_client_and_tokens_encrypted_and_absent_from_public_state(self):
        self.connect()
        with Session() as db:
            for item in db.scalars(select(IntegrationSecret)):
                self.assertNotIn('FAKE_',item.cipher)
        payload=self.client.get('/api/state').text
        for secret in ('FAKE_CLIENT_SECRET','FAKE_ACCESS','FAKE_REFRESH'):
            self.assertNotIn(secret,payload)
        self.assertTrue(self.client.get('/api/youtube/status').json()['connected'])

    def test_config_rejects_wrong_origin_and_redirect(self):
        response=self.client.post('/api/youtube/config',files={'file':('client.json',json.dumps(self.document),'application/json')},headers={'Origin':'https://evil.example'})
        self.assertEqual(response.status_code,403)
        bad={'web':{**self.document['web'],'redirect_uris':['http://wrong.example/callback']}}
        response=self.client.post('/api/youtube/config',files={'file':('client.json',json.dumps(bad),'application/json')})
        self.assertEqual(response.status_code,422)

    def test_oauth_state_is_browser_bound_single_use_and_requests_pkce(self):
        configure_client(self.document)
        url,browser=begin_connection();params=parse_qs(urlparse(url).query);state=params['state'][0]
        self.assertEqual(params['code_challenge_method'],['S256'])
        self.assertEqual(params['access_type'],['offline'])
        with self.assertRaises(ValueError):
            finish_connection('code',state,'wrong-browser')
        def handler(request):
            if request.url.path=='/token':
                self.assertIn(b'code_verifier=',request.content)
                return httpx.Response(200,json={'access_token':'FAKE_ACCESS','refresh_token':'FAKE_REFRESH','scope':SCOPES,'expires_in':3600})
            return httpx.Response(200,json={'items':[{'id':'UCfixture','snippet':{'title':'Fixture channel'}}]})
        with patch('youtube_auth.httpx.Client',side_effect=lambda **kwargs:RealClient(transport=httpx.MockTransport(handler),**kwargs)):
            self.assertTrue(finish_connection('code',state,browser)['connected'])
        with self.assertRaises(ValueError):
            finish_connection('code',state,browser)

    def test_access_token_refresh_preserves_refresh_token(self):
        self.connect()
        with Session.begin() as db:
            account=read_secret('youtube_account',db);account['expires_at']=0;write_secret(db,'youtube_account',account)
        def handler(request):
            self.assertIn(b'refresh_token=FAKE_REFRESH',request.content)
            return httpx.Response(200,json={'access_token':'NEW_ACCESS','expires_in':3600})
        with patch('youtube_auth.httpx.Client',side_effect=lambda **kwargs:RealClient(transport=httpx.MockTransport(handler),**kwargs)):
            self.assertEqual(access_token('UCfixture'),'NEW_ACCESS')
        self.assertEqual(read_secret('youtube_account')['refresh_token'],'FAKE_REFRESH')

    def test_schedule_does_not_claim_youtube_upload_when_not_connected(self):
        response=self.client.post('/api/schedule',json={'ids':[self.clip_id],'starts_at':self.when.isoformat(),'delivery':'youtube'})
        self.assertEqual(response.status_code,409)
        with Session() as db:
            self.assertIsNone(db.get(Publication,self.clip_id))

    def test_schedule_creates_one_upload_and_does_not_remove_active_upload(self):
        self.connect()
        body={'ids':[self.clip_id],'starts_at':self.when.isoformat(),'delivery':'youtube'}
        for _ in range(2):
            self.assertEqual(self.client.post('/api/schedule',json=body).status_code,200)
        with Session() as db:
            self.assertEqual(len(list(db.scalars(select(YouTubeUpload).where(YouTubeUpload.clip_id==self.clip_id)))),1)
        self.assertEqual(claim_upload(),self.clip_id)
        self.assertEqual(self.client.delete(f'/api/schedule/{self.clip_id}').status_code,409)

    def test_cross_origin_request_cannot_schedule_youtube_uploads(self):
        self.connect()
        response=self.client.post('/api/schedule',json={'ids':[self.clip_id],'starts_at':self.when.isoformat(),'delivery':'youtube'},headers={'Origin':'https://evil.example'})
        self.assertEqual(response.status_code,403)
        with Session() as db:
            self.assertIsNone(db.get(YouTubeUpload,self.clip_id))

    def test_chunk_upload_persists_session_and_video_id_without_duplicate_upload(self):
        self.connect();self.path.write_bytes(b'a'*(CHUNK+7));self.plan()
        calls=[]
        def handler(request):
            calls.append(request)
            if request.method=='POST':
                body=json.loads(request.content)
                self.assertEqual(body['status']['privacyStatus'],'private')
                self.assertEqual(body['status']['publishAt'],self.when.isoformat())
                return httpx.Response(200,headers={'Location':URI})
            with Session() as db:
                self.assertNotIn('SECRET_SESSION',db.get(YouTubeUpload,self.clip_id).session_cipher)
            if request.headers['Content-Range'].startswith('bytes 0-'):
                return httpx.Response(308,headers={'Range':f'bytes=0-{CHUNK-1}'})
            return httpx.Response(200,json=self.completed())
        with self.transport(handler):
            upload_one(self.clip_id)
            upload_one(self.clip_id)
        self.assertEqual(sum(r.method=='POST' for r in calls),1)
        self.assertEqual([len(r.content) for r in calls if r.method=='PUT'],[CHUNK,7])
        with Session() as db:
            item=db.get(YouTubeUpload,self.clip_id)
            self.assertEqual(item.youtube_id,VIDEO_ID)
            self.assertEqual(item.status,'scheduled')
            self.assertEqual(db.get(Publication,self.clip_id).youtube_id,VIDEO_ID)
        self.assertNotIn('SECRET_SESSION',self.client.get('/api/state').text)

    def test_lost_final_response_queries_existing_session_instead_of_uploading_again(self):
        self.connect();self.plan();calls=[]
        def handler(request):
            calls.append(request)
            if request.method=='POST':
                return httpx.Response(200,headers={'Location':URI})
            if request.headers['Content-Range'].startswith('bytes */'):
                return httpx.Response(200,json=self.completed())
            raise httpx.ReadTimeout('ACK lost',request=request)
        with self.transport(handler):
            upload_one(self.clip_id)
            with Session() as db:
                self.assertEqual(db.get(YouTubeUpload,self.clip_id).status,'retry')
            upload_one(self.clip_id)
        self.assertEqual(sum(r.method=='POST' for r in calls),1)
        self.assertEqual(sum(r.method=='PUT' and not r.headers['Content-Range'].startswith('bytes */') for r in calls),1)
        with Session() as db:
            self.assertEqual(db.get(YouTubeUpload,self.clip_id).youtube_id,VIDEO_ID)

    def test_expired_session_requires_verification_not_new_upload(self):
        self.connect();self.plan()
        with Session.begin() as db:
            item=db.get(YouTubeUpload,self.clip_id)
            stat=self.path.stat();item.file_size=stat.st_size;item.fingerprint=f'{stat.st_size}:{stat.st_mtime_ns}'
            item.session_cipher=seal(URI)
        def handler(request):
            self.assertEqual(request.method,'PUT')
            return httpx.Response(404)
        with self.transport(handler),self.assertLogs('vi-clips.youtube-publish',level='WARNING'):
            upload_one(self.clip_id)
        with Session() as db:
            self.assertEqual(db.get(YouTubeUpload,self.clip_id).status,'needs_verification')
            self.assertEqual(unseal(db.get(YouTubeUpload,self.clip_id).session_cipher),URI)
        self.assertEqual(self.client.post(f'/api/youtube/uploads/{self.clip_id}/retry').status_code,409)

    def test_wrong_channel_cannot_claim_or_upload(self):
        self.connect();self.plan()
        with Session.begin() as db:
            account=read_secret('youtube_account',db);account['channel_id']='UCother';write_secret(db,'youtube_account',account)
        self.assertIsNone(claim_upload())
        with self.transport(lambda request:(_ for _ in ()).throw(AssertionError('No upload allowed'))),self.assertLogs('vi-clips.youtube-publish',level='WARNING'):
            upload_one(self.clip_id)
        with Session() as db:
            self.assertEqual(db.get(YouTubeUpload,self.clip_id).status,'auth_required')
        self.assertEqual(self.client.post(f'/api/youtube/uploads/{self.clip_id}/retry').status_code,409)

    def test_private_upload_does_not_set_publish_at_and_recovers_after_restart(self):
        self.connect();self.plan('private')
        self.assertEqual(claim_upload(),self.clip_id)
        recover_uploads()
        self.assertEqual(claim_upload(),self.clip_id)
        def handler(request):
            if request.method=='POST':
                self.assertNotIn('publishAt',json.loads(request.content)['status'])
                return httpx.Response(200,headers={'Location':URI})
            return httpx.Response(200,json={'id':VIDEO_ID,'status':{'privacyStatus':'private'}})
        with self.transport(handler):
            upload_one(self.clip_id)
        with Session() as db:
            self.assertEqual(db.get(YouTubeUpload,self.clip_id).status,'uploaded_private')

    def test_past_schedule_never_publishes_immediately(self):
        self.connect();self.plan()
        with Session.begin() as db:
            db.get(Publication,self.clip_id).scheduled_at=datetime.now(timezone.utc)-timedelta(minutes=1)
        with self.transport(lambda request:(_ for _ in ()).throw(AssertionError('No upload allowed'))),self.assertLogs('vi-clips.youtube-publish',level='WARNING'):
            upload_one(self.clip_id)
        with Session() as db:
            self.assertEqual(db.get(YouTubeUpload,self.clip_id).status,'needs_reschedule')

    def test_remote_status_refresh_reports_published_without_reupload(self):
        self.connect();self.plan()
        with Session.begin() as db:
            item=db.get(YouTubeUpload,self.clip_id);item.youtube_id=VIDEO_ID;item.status='scheduled'
        with self.transport(lambda request:httpx.Response(200,json={'items':[{'id':VIDEO_ID,'status':{'privacyStatus':'public','uploadStatus':'processed'}}]})):
            self.assertEqual(refresh_remote(self.clip_id)['status'],'published')
