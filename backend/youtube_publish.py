import logging
import re
from datetime import datetime,timedelta,timezone
from urllib.parse import urlparse
import httpx
from sqlalchemy import select,or_
from db import DATA,Session,Clip,Publication,YouTubeUpload
from youtube_auth import read_secret,access_token,seal,unseal

log=logging.getLogger('vi-clips.youtube-publish')
CHUNK=8*1024*1024
ACTIVE={'pending','uploading','retry'}

class UploadProblem(Exception):
    def __init__(self,message,status='error'):
        super().__init__(message);self.status=status

def aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

def queue_upload(db,clip_id,mode):
    if mode not in {'scheduled','private'}:
        raise ValueError('Choose scheduled or private YouTube upload')
    account=read_secret('youtube_account',db)
    if not account:
        raise ValueError('Connect your YouTube channel before uploading')
    item=db.get(YouTubeUpload,clip_id)
    if item:
        if item.channel_id!=account['channel_id']:
            raise ValueError('This clip already belongs to an upload for another channel')
        if item.mode!=mode:
            raise ValueError('This clip already has a different YouTube upload mode')
        return item
    item=YouTubeUpload(clip_id=clip_id,channel_id=account['channel_id'],mode=mode,status='pending')
    db.add(item)
    publication=db.get(Publication,clip_id)
    if publication:
        publication.status='upload_pending'
    return item

def public_upload(item):
    return {key:getattr(item,key) for key in ('clip_id','channel_id','mode','status','offset','file_size','youtube_id','error','attempts','next_attempt','remote')}

def recover_uploads():
    with Session.begin() as db:
        for item in db.scalars(select(YouTubeUpload).where(YouTubeUpload.status=='uploading')):
            item.status='pending'

def claim_upload():
    account=read_secret('youtube_account')
    if not account:
        return None
    with Session.begin() as db:
        item=db.scalar(select(YouTubeUpload).join(Clip,Clip.id==YouTubeUpload.clip_id)
            .join(Publication,Publication.clip_id==Clip.id)
            .where(YouTubeUpload.status.in_(['pending','retry']),YouTubeUpload.channel_id==account['channel_id'],
                   Clip.status=='exported',Clip.export.is_not(None),
                   or_(YouTubeUpload.next_attempt.is_(None),YouTubeUpload.next_attempt<=datetime.now(timezone.utc)))
            .order_by(Publication.scheduled_at).limit(1).with_for_update(of=YouTubeUpload,skip_locked=True))
        if item is None:
            return None
        item.status='uploading';item.error=None
        db.get(Publication,item.clip_id).status='uploading'
        return item.clip_id

def session_url(value):
    parsed=urlparse(value)
    if parsed.scheme!='https' or parsed.hostname not in {'www.googleapis.com','youtube.googleapis.com','upload.youtube.com'} or parsed.username or parsed.password or parsed.port not in (None,443):
        raise UploadProblem('Google returned an unexpected upload-session address')
    if not parsed.path.startswith(('/upload/youtube/','/resumable/')) or parsed.fragment:
        raise UploadProblem('Google returned an unexpected upload-session path')
    return value

def request(http,method,url,channel_id,**kwargs):
    headers=kwargs.pop('headers',{})
    for attempt in range(2):
        try:
            token=access_token(channel_id,force=attempt==1)
        except ValueError as exc:
            raise UploadProblem(str(exc),'auth_required') from exc
        response=http.request(method,url,headers={**headers,'Authorization':'Bearer '+token},**kwargs)
        if response.status_code!=401:
            return response
    raise UploadProblem('YouTube authorization was rejected. Reconnect your channel.','auth_required')

def check_response(response):
    if response.status_code in {200,201,308}:
        return
    if response.status_code in {404,410}:
        raise UploadProblem('This upload session expired or cannot be verified. Check your channel before starting another upload.','needs_verification')
    if response.status_code in {429,500,502,503,504}:
        raise UploadProblem('YouTube is temporarily unavailable; the existing upload session will be resumed.','retry')
    try:
        message=response.json().get('error',{}).get('message','YouTube rejected the upload')
    except (ValueError,AttributeError):
        message='YouTube rejected the upload'
    raise UploadProblem(str(message)[:800])

def received_offset(response,total):
    match=re.fullmatch(r'bytes=0-(\d+)',response.headers.get('Range',''))
    if response.headers.get('Range') and not match:
        raise UploadProblem('Google returned an invalid resume offset. Verify the upload before retrying.','needs_verification')
    offset=int(match.group(1))+1 if match else 0
    if offset>total:
        raise UploadProblem('Google returned a resume offset beyond this video.','needs_verification')
    return offset

def save_offset(id,offset):
    with Session.begin() as db:
        db.get(YouTubeUpload,id).offset=offset

def complete(id,result):
    video_id=result.get('id')
    if not video_id or not re.fullmatch(r'[A-Za-z0-9_-]{11}',video_id):
        raise UploadProblem('The upload completed without a verifiable YouTube video ID. Check the channel before retrying.','needs_verification')
    with Session.begin() as db:
        item=db.get(YouTubeUpload,id);publication=db.get(Publication,id)
        if item.youtube_id and item.youtube_id!=video_id:
            raise UploadProblem('YouTube returned a different video ID. Verify the channel before continuing.','needs_verification')
        remote=result.get('status',{})
        item.youtube_id=video_id;item.offset=item.file_size;item.error=None;item.next_attempt=None
        item.remote={key:remote[key] for key in ('privacyStatus','publishAt','uploadStatus') if key in remote}
        item.status='uploaded_private' if item.mode=='private' else ('scheduled' if remote.get('publishAt') else 'uploaded_check_schedule')
        item.session_cipher=None
        if publication:
            publication.youtube_id=video_id;publication.status=item.status
    log.info('YouTube upload completed for clip %s: %s',id,video_id)

def upload_one(id,stop=None):
    try:
        with Session() as db:
            item=db.get(YouTubeUpload,id);clip=db.get(Clip,id);publication=db.get(Publication,id)
            if not item or not publication or item.youtube_id:
                return
            path=(DATA/(clip.export or '')).resolve()
            if path.parent!=DATA or not path.is_file():
                raise UploadProblem('The rendered video is missing. Restore the export before uploading.')
            stat=path.stat();fingerprint=f'{stat.st_size}:{stat.st_mtime_ns}'
            if item.session_cipher and item.fingerprint!=fingerprint:
                raise UploadProblem('The rendered file changed during upload. Verify the existing session before retrying.','needs_verification')
            channel_id=item.channel_id;mode=item.mode;planned=aware(publication.scheduled_at)
            uri=session_url(unseal(item.session_cipher)) if item.session_cipher else None
            item.file_size=stat.st_size;item.fingerprint=fingerprint
            db.commit()
            title=clip.title
        with httpx.Client(timeout=httpx.Timeout(120,connect=20),follow_redirects=False) as http:
            # Always query a persisted session before sending another byte.
            offset=0
            if uri:
                response=request(http,'PUT',uri,channel_id,content=b'',headers={'Content-Length':'0','Content-Range':f'bytes */{stat.st_size}'})
                check_response(response)
                if response.status_code in {200,201}:
                    complete(id,response.json());return
                offset=received_offset(response,stat.st_size);save_offset(id,offset)
            if mode=='scheduled' and planned<=datetime.now(timezone.utc):
                raise UploadProblem('The scheduled time has passed. Set a future time before uploading.','needs_reschedule')
            if not uri:
                status={'privacyStatus':'private','selfDeclaredMadeForKids':False}
                if mode=='scheduled':
                    status['publishAt']=planned.astimezone(timezone.utc).isoformat()
                payload={'snippet':{'title':title,'description':'Created with VI Clips.','categoryId':'22'},'status':status}
                response=request(http,'POST','https://www.googleapis.com/upload/youtube/v3/videos',channel_id,
                    params={'uploadType':'resumable','part':'snippet,status','notifySubscribers':'false'},json=payload,
                    headers={'X-Upload-Content-Type':'video/mp4','X-Upload-Content-Length':str(stat.st_size)})
                check_response(response)
                uri=session_url(response.headers.get('Location',''))
                with Session.begin() as db:
                    db.get(YouTubeUpload,id).session_cipher=seal(uri)
            with path.open('rb') as video:
                while offset<stat.st_size:
                    if stop and stop.is_set():
                        raise UploadProblem('Upload paused for shutdown; the same session will resume.','retry')
                    if mode=='scheduled' and planned<=datetime.now(timezone.utc):
                        raise UploadProblem('The scheduled time passed during upload. Verify the session before rescheduling.','needs_reschedule')
                    video.seek(offset);chunk=video.read(min(CHUNK,stat.st_size-offset))
                    response=request(http,'PUT',uri,channel_id,content=chunk,
                        headers={'Content-Type':'video/mp4','Content-Length':str(len(chunk)),
                                 'Content-Range':f'bytes {offset}-{offset+len(chunk)-1}/{stat.st_size}'})
                    check_response(response)
                    if response.status_code in {200,201}:
                        complete(id,response.json());return
                    next_offset=received_offset(response,stat.st_size)
                    if next_offset<=offset:
                        raise UploadProblem('YouTube has not acknowledged this chunk yet. The session will be queried before resuming.','retry')
                    offset=next_offset;save_offset(id,offset)
            raise UploadProblem('All bytes were received; waiting for YouTube to confirm the video ID.','retry')
    except Exception as exc:
        status=exc.status if isinstance(exc,UploadProblem) else ('retry' if isinstance(exc,httpx.HTTPError) else 'error')
        message=str(exc) if isinstance(exc,UploadProblem) else 'Upload interrupted. Retry resumes the saved session; no new upload is started.'
        with Session.begin() as db:
            item=db.get(YouTubeUpload,id)
            if item is None or item.youtube_id:
                return
            item.attempts+=1
            if status=='retry' and item.attempts>=5:
                status='error'
            item.status=status;item.error=message[:1000]
            item.next_attempt=datetime.now(timezone.utc)+timedelta(seconds=min(300,5*2**min(item.attempts,6))) if status=='retry' else None
            publication=db.get(Publication,id)
            if publication:
                publication.status='upload_'+status
        log.warning('YouTube upload paused for clip %s: %s',id,message)

def refresh_remote(id):
    with Session() as db:
        item=db.get(YouTubeUpload,id)
        if not item or not item.youtube_id:
            raise ValueError('This clip does not have a YouTube video ID yet')
        channel_id,video_id=item.channel_id,item.youtube_id
    with httpx.Client(timeout=30) as http:
        response=request(http,'GET','https://www.googleapis.com/youtube/v3/videos',channel_id,
                         params={'part':'status,processingDetails','id':video_id})
    check_response(response)
    videos=response.json().get('items',[])
    if not videos:
        raise ValueError('YouTube did not return this video. Check its channel and permissions.')
    remote=videos[0].get('status',{})
    processing=videos[0].get('processingDetails',{})
    with Session.begin() as db:
        item=db.get(YouTubeUpload,id)
        item.remote={key:remote[key] for key in ('privacyStatus','publishAt','uploadStatus') if key in remote}
        item.error=None
        if remote.get('uploadStatus') in {'failed','rejected'} or processing.get('processingStatus')=='failed':
            item.status='youtube_rejected';item.error=remote.get('rejectionReason') or remote.get('failureReason') or processing.get('processingFailureReason') or 'YouTube could not process this video'
        elif remote.get('privacyStatus')=='public':
            item.status='published'
        elif remote.get('publishAt'):
            item.status='scheduled'
        else:
            item.status='uploaded_private'
        publication=db.get(Publication,id)
        if publication:
            publication.status=item.status
        return public_upload(item)
