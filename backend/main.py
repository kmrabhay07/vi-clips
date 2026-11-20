import csv
import os
import io
import re
import uuid
import zipfile
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote
from fastapi import FastAPI, UploadFile, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from layout import DualCrop
from sqlalchemy import select, delete
from db import DATA, Session, Source, Clip, Job, Publication, Rejection, SourceProgress, YouTubeUpload, init
from retention import cancel_rejection, empty_rejected
from youtube import video_id
from youtube_cookies import cookie_status, save_cookies, remove_cookies
from media import probe, captions_for, retime_captions, run
from caption_events import word_events
from published_cleanup import policy_status,cleanup_published
from db import CleanupPolicy,PublishedHistory
from settings import performance
from storage import ensure_space, StorageFull
from youtube_auth import connection_status,configure_client,begin_connection,finish_connection,disconnect,base_url
from youtube_publish import queue_upload,public_upload,refresh_remote,UploadProblem

def uid():
    return uuid.uuid4().hex

def youtube_id(url):
    try:
        return video_id(url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

@asynccontextmanager
async def lifespan(app):
    init()
    yield

app = FastAPI(title='VI Clips API', lifespan=lifespan)

@app.exception_handler(StorageFull)
async def storage_full(request: Request, exc: StorageFull):
    return JSONResponse(status_code=507,content={'detail':str(exc)})

@app.get('/api/health')
def health():
    return {'ok': True}

def get(db, model, id):
    key = list(model.__table__.primary_key.columns)[0]
    obj = db.scalar(select(model).where(key == id).with_for_update())
    if not obj:
        raise HTTPException(404, 'Not found')
    return obj

def serialize(obj):
    return {column.name: getattr(obj, column.name) for column in obj.__table__.columns}

def enqueue(db, kind, target):
    db.add(Job(id=uid(), kind=kind, target=target))

def ensure_export(db, clip):
    active = db.scalar(select(Job).where(Job.target == clip.id, Job.kind == 'export', Job.status.in_(['queued','running'])))
    if not active and clip.export and (DATA / clip.export).is_file():
        clip.status = 'exported'
        return False
    clip.export, clip.status = None, 'rendering'
    if not active:
        for failed in db.scalars(select(Job).where(Job.target == clip.id, Job.kind == 'export', Job.status == 'failed')):
            failed.status = 'superseded'
        enqueue(db, 'export', clip.id)
    publication = db.get(Publication, clip.id)
    if publication:
        publication.status = 'rendering'
    return True

@app.get('/api/state')
def state():
    with Session() as db:
        result = {'performance': performance.public(), **{name: [serialize(x) for x in db.scalars(select(model))] for name, model in [('clips', Clip), ('jobs', Job), ('publications', Publication)]}}
        # Polling the dashboard must not fetch or serialize entire transcripts/signals.
        columns=[column for column in Source.__table__.columns if column.name not in {'words','analysis_cache'}]
        result['sources']=[dict(row) for row in db.execute(select(*columns)).mappings()]
        deadlines = {r.clip_id: r.delete_after for r in db.scalars(select(Rejection))}
        for clip in result['clips']:
            date = deadlines.get(clip['id'])
            clip['delete_after'] = (date.replace(tzinfo=timezone.utc) if date and date.tzinfo is None else date)
        progress={p.source_id:p.details for p in db.scalars(select(SourceProgress))}
        for source in result['sources']:
            source['progress']=progress.get(source['id'])
        result['publications'].sort(key=lambda item:item['scheduled_at'])
        result['youtube']=connection_status()
        result['deployment']=os.getenv('DEPLOYMENT_LABEL','Local processing')
        result['cleanup_policy']=policy_status()
        result['published_history']=[serialize(item) for item in db.scalars(select(PublishedHistory).order_by(PublishedHistory.cleaned_at.desc()).limit(200))]
        result['import_cookies']=cookie_status()
        result['uploads']=[public_upload(item) for item in db.scalars(select(YouTubeUpload))]
        return result

def youtube_origin(request):
    origin=request.headers.get('origin')
    allowed={base_url()}
    parsed=urlparse(base_url())
    if parsed.hostname in {'localhost','127.0.0.1'}:
        allowed.update({f'{parsed.scheme}://{host}'+(f':{parsed.port}' if parsed.port else '') for host in ('localhost','127.0.0.1')})
    if origin and origin not in allowed:
        raise HTTPException(403,'YouTube settings must be changed from this app')

@app.get('/api/youtube/status')
def youtube_status():
    return connection_status()

class CleanupSettings(BaseModel):
    clips: bool = False
    sources: bool = False

@app.put('/api/cleanup-policy')
def update_cleanup_policy(request:Request,body:CleanupSettings):
    youtube_origin(request)
    if body.sources and not body.clips:
        raise HTTPException(422,'Enable published clip cleanup before source cleanup')
    with Session.begin() as db:
        policy=db.get(CleanupPolicy,1)
        if not policy:
            policy=CleanupPolicy(id=1);db.add(policy)
        policy.clips,policy.sources=body.clips,body.sources
    return policy_status()

@app.post('/api/cleanup-published')
def clean_published_now(request:Request):
    youtube_origin(request)
    return cleanup_published()

@app.get('/api/youtube/import-cookies')
def import_cookie_status():
    return cookie_status()

@app.post('/api/youtube/import-cookies')
async def import_cookies(request: Request, file: UploadFile):
    youtube_origin(request)
    try:
        raw = await file.read(1048577)
        if len(raw) > 1048576:
            raise HTTPException(413, 'Cookie export must be smaller than 1 MB')
        return save_cookies(raw)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        await file.close()

@app.delete('/api/youtube/import-cookies')
def delete_import_cookies(request: Request):
    youtube_origin(request)
    return remove_cookies()

@app.post('/api/youtube/config')
async def youtube_config(request: Request,file: UploadFile):
    youtube_origin(request)
    try:
        data=await file.read(65537)
        if len(data)>65536:
            raise HTTPException(413,'OAuth client JSON must be smaller than 64 KB')
        import json
        configure_client(json.loads(data))
        return connection_status()
    except (ValueError,TypeError,AttributeError) as exc:
        raise HTTPException(422,str(exc)) from exc
    finally:
        await file.close()

@app.get('/api/youtube/connect')
def youtube_connect():
    try:
        url,browser=begin_connection()
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc
    response=RedirectResponse(url,status_code=302)
    response.set_cookie('vi_youtube_oauth',browser,max_age=600,httponly=True,samesite='lax',secure=base_url().startswith('https://'))
    response.headers['Cache-Control']='no-store'
    return response

class OAuthCallback(BaseModel):
    code: str = Field(min_length=1,max_length=4096)
    state: str = Field(min_length=1,max_length=512)

@app.post('/api/youtube/callback')
def youtube_callback(request: Request,body: OAuthCallback):
    youtube_origin(request)
    try:
        status=finish_connection(body.code,body.state,request.cookies.get('vi_youtube_oauth',''))
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from exc
    response=JSONResponse(status)
    response.delete_cookie('vi_youtube_oauth')
    response.headers['Cache-Control']='no-store'
    return response

@app.post('/api/youtube/disconnect')
def youtube_disconnect(request: Request):
    youtube_origin(request)
    disconnect()
    return connection_status()

@app.post('/api/youtube/uploads/{id}/retry')
def youtube_retry(request: Request,id: str):
    youtube_origin(request)
    with Session.begin() as db:
        get(db,Clip,id)
        item=get(db,YouTubeUpload,id)
        if item.youtube_id:
            return {'ok':True,'youtube_id':item.youtube_id}
        if item.status in {'needs_verification','needs_reschedule'}:
            raise HTTPException(409,item.error)
        if item.status in {'pending','uploading','retry'}:
            return {'ok':True}
        connection=connection_status()
        if not connection['connected']:
            raise HTTPException(409,'Connect your channel before retrying')
        if connection['channel_id']!=item.channel_id:
            raise HTTPException(409,'Reconnect the original YouTube channel for this upload')
        item.status,item.error,item.next_attempt,item.attempts='pending',None,None,0
        get(db,Publication,id).status='upload_pending'
    return {'ok':True}

@app.post('/api/youtube/uploads/{id}/refresh')
def youtube_refresh(request: Request,id: str):
    youtube_origin(request)
    try:
        return refresh_remote(id)
    except (ValueError,UploadProblem) as exc:
        raise HTTPException(409,str(exc)) from exc

@app.delete('/api/clips/rejected')
def empty_rejected_clips():
    return empty_rejected()

@app.delete('/api/sources/{id}')
def delete_source(id: str):
    if not re.fullmatch(r'[a-f0-9]{32}', id):
        raise HTTPException(422, 'Invalid source ID')
    with Session.begin() as db:
        source = get(db, Source, id)
        clips = list(db.scalars(select(Clip).where(Clip.source_id == id).order_by(Clip.id).with_for_update()))
        ids = [clip.id for clip in clips]
        targets = [id, *ids]
        if db.scalar(select(Job).where(Job.target.in_(targets), Job.status.in_(['queued', 'running']))):
            raise HTTPException(409, 'Wait for source processing and clip renders to finish before deleting')
        if db.scalar(select(Publication).where(Publication.clip_id.in_(ids))) or db.scalar(select(YouTubeUpload).where(YouTubeUpload.clip_id.in_(ids))):
            raise HTTPException(409, 'This source has clips in the publishing queue. Remove them from the queue first; uploaded videos remain on YouTube.')
        paths = [DATA / f'{id}{suffix}' for suffix in ('.mp4', '.wav')]
        for clip_id in ids:
            if not re.fullmatch(r'[a-f0-9]{32}', clip_id):
                raise HTTPException(409, 'Invalid clip storage ID')
            paths.extend(DATA / f'{clip_id}{suffix}' for suffix in ('-preview.mp4', '-preview.ass', '-export.mp4', '-export.ass'))
        for path in paths:
            if path.is_symlink() or path.resolve().parent != DATA:
                raise HTTPException(409, 'Unsafe media storage path')
        cache = None
        if source.youtube_id and re.fullmatch(r'[A-Za-z0-9_-]{11}', source.youtube_id):
            cache = DATA / 'youtube-cache' / source.youtube_id
            if (DATA / 'youtube-cache').is_symlink() or cache.is_symlink() or cache.resolve().parent != (DATA / 'youtube-cache').resolve():
                raise HTTPException(409, 'Unsafe import cache path')
            if cache.exists() and any(p.is_symlink() for p in cache.rglob('*')):
                raise HTTPException(409, 'Unsafe import cache contents')
        for path in paths:
            path.unlink(missing_ok=True)
        if cache and cache.exists():
            shutil.rmtree(cache)
        db.execute(delete(Job).where(Job.target.in_(targets)))
        db.execute(delete(Rejection).where(Rejection.clip_id.in_(ids)))
        db.execute(delete(SourceProgress).where(SourceProgress.source_id == id))
        db.execute(delete(Clip).where(Clip.source_id == id))
        db.delete(source)
    return {'deleted': True, 'clips_deleted': len(ids)}

@app.post('/api/jobs/{id}/retry')
def retry(id: str):
    with Session.begin() as db:
        job = get(db, Job, id)
        if job.status != 'failed':
            raise HTTPException(409, 'Only failed jobs can be retried')
        if job.kind in {'analyze', 'import_youtube', 'highlights'}:
            source = get(db, Source, job.target)
            if job.kind == 'analyze' and not (DATA / f'{source.id}.mp4').is_file():
                raise HTTPException(409, 'The original file is missing. Upload the original MP4.')
        else:
            clip = get(db, Clip, job.target)
        if db.scalar(select(Job).where(Job.target == job.target, Job.status.in_(['queued', 'running']))):
            raise HTTPException(409, 'This target already has a processing job')
        if job.kind in {'analyze', 'import_youtube', 'highlights'}:
            source.status, source.error = 'queued', None
        elif job.kind == 'export':
            if clip.status != 'approved':
                raise HTTPException(409, 'Approve the clip before retrying an export')
            clip.status = 'rendering'
            publication = db.get(Publication, clip.id)
            if publication:
                publication.status = 'rendering'
        job.status, job.error = 'queued', None
    return {'ok': True}

@app.post('/api/sources')
async def upload(file: UploadFile, prompt: str = Form('wild chases and funny crashes'), youtube_url: str = Form(''), layout_setup: bool = Form(False)):
    if not file.filename or not file.filename.lower().endswith('.mp4'):
        raise HTTPException(422, 'Upload an original MP4 file')
    yt = youtube_id(youtube_url) if youtube_url else None
    id = uid()
    path = DATA / f'{id}.mp4'
    try:
        expected=getattr(file,'size',None)
        if expected is not None:
            if expected>20*1024**3:
                raise HTTPException(413,'Maximum upload size is 20 GB')
            ensure_space(DATA,expected)
        size = 0
        with path.open('wb') as destination:
            while chunk := await file.read(4 * 1024 * 1024):
                size += len(chunk)
                if size > 20 * 1024 ** 3:
                    raise HTTPException(413, 'Maximum upload size is 20 GB')
                ensure_space(DATA,len(chunk))
                await run_in_threadpool(destination.write,chunk)
        if expected is not None and size!=expected:
            raise HTTPException(400,'Upload interrupted: the received file is incomplete')
        duration, _ = await run_in_threadpool(probe,path)
        if duration < 20:
            raise HTTPException(422, 'Upload at least 20 seconds of gameplay')
        with Session.begin() as db:
            existing = db.scalar(select(Source).where(Source.youtube_id == yt).with_for_update()) if yt else None
            if existing:
                if existing.status != 'failed' or (DATA / f'{existing.id}.mp4').exists() or db.scalar(select(Job).where(Job.target == existing.id, Job.status.in_(['queued', 'running']))):
                    raise HTTPException(409, 'This YouTube video already has a stored source or active job. Open it in the source library.')
                # Attach an uploaded original when a link download failed, retaining its URL.
                destination = DATA / f'{existing.id}.mp4'
                path.replace(destination)
                path = destination
                existing.name, existing.duration, existing.prompt = Path(file.filename).name[:250], duration, prompt[:500]
                existing.layout_pending = layout_setup
                existing.status, existing.error = ('awaiting_layout' if layout_setup else 'queued'), None
                for job in db.scalars(select(Job).where(Job.target == existing.id, Job.status == 'failed')):
                    job.status, job.error = 'superseded', None
                if not layout_setup:
                    enqueue(db, 'analyze', existing.id)
                return {'id': existing.id}
            source = Source(id=id, name=Path(file.filename).name[:250], youtube_id=yt, prompt=prompt[:500], duration=duration, layout_pending=layout_setup, status='awaiting_layout' if layout_setup else 'queued')
            db.add(source)
            if not layout_setup:
                enqueue(db, 'analyze', id)
        return {'id': id}
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

class UploadMetadata(BaseModel):
    filename: str = Field(min_length=1,max_length=250)
    file_size: int = Field(gt=0)
    prompt: str = Field(default='wild chases and funny crashes',max_length=500)
    youtube_url: str = Field(default='',max_length=2048)
    layout_setup: bool = False

@app.post('/api/sources/raw')
async def raw_upload(request: Request):
    # Bypass multipart's full temporary-file spool; write each chunk once.
    import json
    header=request.headers.get('x-vi-upload','')
    if len(header)>8192:
        raise HTTPException(422,'Upload metadata is too large')
    try:
        metadata=UploadMetadata.model_validate(json.loads(unquote(header)))
    except (ValueError,TypeError):
        raise HTTPException(422,'Invalid upload metadata') from None
    class StreamFile:
        filename=metadata.filename
        size=metadata.file_size
        def __init__(self):
            self.stream=request.stream().__aiter__()
        async def read(self,size):
            return await anext(self.stream,b'')
        async def close(self):
            pass
    return await upload(StreamFile(),metadata.prompt,metadata.youtube_url,metadata.layout_setup)

class Link(BaseModel):
    url: str
    prompt: str = Field(default='wild chases and funny crashes', max_length=500)
    regenerate: bool = False
    layout_setup: bool = False
    use_browser_cookies: bool = False

@app.post('/api/resolve')
def resolve(body: Link):
    if body.use_browser_cookies and not cookie_status()['configured']:
        raise HTTPException(422, 'Connect your YouTube browser session before importing')
    with Session.begin() as db:
        source = db.scalar(select(Source).where(Source.youtube_id == youtube_id(body.url)).with_for_update())
        if not source:
            source = Source(id=uid(), name=f'YouTube {youtube_id(body.url)}', youtube_id=youtube_id(body.url), prompt=body.prompt, layout_pending=body.layout_setup, use_browser_cookies=body.use_browser_cookies)
            db.add(source)
            enqueue(db, 'import_youtube', source.id)
            return {'id': source.id, 'status': 'queued', 'importing': True}
        active = db.scalar(select(Job).where(Job.target == source.id, Job.status.in_(['queued', 'running'])))
        if not active:
            source.use_browser_cookies = body.use_browser_cookies
        if body.layout_setup and not active and (DATA / f'{source.id}.mp4').is_file():
            source.layout_pending, source.status = True, 'awaiting_layout'
            source.prompt = body.prompt
            return {'id': source.id, 'status': source.status, 'importing': False, 'reused_source': True}
        if body.layout_setup and source.status == 'failed' and not active:
            source.layout_pending = True
        if source.status == 'failed' and not active:
            failed = db.scalar(select(Job).where(Job.target == source.id, Job.kind.in_(['analyze', 'import_youtube', 'highlights']), Job.status == 'failed').order_by(Job.created.desc()))
            if failed:
                failed.status, failed.error = 'queued', None
                source.status, source.error = 'queued', None
                active = failed
        if source.status == 'ready' and not active and (body.regenerate or not db.scalar(select(Clip).where(Clip.source_id == source.id))):
            source.status = 'queued'
            source.prompt = body.prompt
            enqueue(db, 'highlights', source.id)
        return {'id': source.id, 'status': source.status, 'importing': source.status != 'ready', 'reused_source': True}

class SourceLayout(BaseModel):
    dual_crop: DualCrop | None = None

@app.post('/api/sources/{id}/early-clips')
def early_source_clips(id: str):
    from early_clips import create_early_clips
    try:
        return create_early_clips(id)
    except ValueError as exc:
        raise HTTPException(404,str(exc)) from exc

@app.get('/api/sources/{id}/layout-frame')
def source_layout_frame(id: str):
    with Session() as db:
        source = db.get(Source, id)
        if not source:
            raise HTTPException(404, 'Source not found')
        path = DATA / f'{source.id}.mp4'
        if not path.is_file():
            raise HTTPException(409, 'Wait for the video download to finish')
        frame = run(['ffmpeg', '-v', 'error', '-threads', str(performance.ffmpeg_threads),
                     '-ss', str(min(1, max(0, source.duration-.1))), '-i', str(path),
                     '-frames:v', '1', '-vf', 'scale=1280:720:force_original_aspect_ratio=decrease',
                     '-threads', '1', '-f', 'image2pipe', '-vcodec', 'mjpeg', '-'])
        if not frame:
            raise HTTPException(422, 'Could not capture a frame from this recording')
        return Response(frame, media_type='image/jpeg', headers={'Cache-Control':'private, max-age=300'})

@app.put('/api/sources/{id}/layout')
def source_layout(id: str, body: SourceLayout):
    with Session.begin() as db:
        source = get(db, Source, id)
        if source.status != 'awaiting_layout' or not source.layout_pending:
            raise HTTPException(409, 'This source is not waiting for layout setup')
        if not (DATA / f'{id}.mp4').is_file():
            raise HTTPException(409, 'Wait for the source download to finish')
        source.dual_crop = body.dual_crop.model_dump() if body.dual_crop else {}
        source.layout_pending, source.status, source.error = False, 'queued', None
        enqueue(db, 'highlights' if source.words else 'analyze', id)
    return {'ok': True}

class Cue(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str = Field(max_length=300)

class CropSegment(BaseModel):
    time: float = Field(ge=0, allow_inf_nan=False)
    x: float = Field(ge=0, le=1, allow_inf_nan=False)
    y: float = Field(ge=0, le=1, allow_inf_nan=False)

class Edit(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    framing: str = 'crop'
    dual_crop: DualCrop | None = None
    crop_x: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)
    crop_y: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)
    crop_segments: list[CropSegment] = Field(default_factory=list, max_length=20)
    caption_style: str = 'highlight'
    caption_scale: float = Field(default=1, ge=0.6, le=1.6, allow_inf_nan=False)
    caption_position: str = 'bottom'
    volume: float = Field(default=1, ge=0, le=2, allow_inf_nan=False)
    audio_muted: bool = False
    captions: list[Cue] = Field(default_factory=list, max_length=150)

class Retime(BaseModel):
    old_start: float = Field(ge=0, allow_inf_nan=False)
    old_end: float = Field(gt=0, allow_inf_nan=False)
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    captions: list[Cue] = Field(default_factory=list, max_length=150)

class CaptionEventsRequest(BaseModel):
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    captions: list[Cue] = Field(default_factory=list, max_length=150)

@app.post('/api/sources/{id}/caption-events')
def preview_caption_events(id: str, body: CaptionEventsRequest):
    with Session() as db:
        source=db.get(Source,id)
        if not source:
            raise HTTPException(404,'Source not found')
        if body.end<=body.start or body.end>source.duration:
            raise HTTPException(422,'Choose a window within the source')
        if any(c.end<=c.start or c.end>body.end-body.start for c in body.captions):
            raise HTTPException(422,'Caption times must be within the clip')
        return {'events':word_events([c.model_dump() for c in body.captions],source.words,body.start)}

@app.post('/api/sources/{id}/retime-captions')
def retime_source_captions(id: str, body: Retime):
    with Session() as db:
        source = db.get(Source,id)
        if not source:
            raise HTTPException(404,'Source not found')
        if body.end <= body.start or body.end > source.duration or body.old_end <= body.old_start:
            raise HTTPException(422,'Choose a window within the source')
        return {'captions':retime_captions([c.model_dump() for c in body.captions],body.old_start,body.old_end,body.start,body.end,source.words)}

def validate(body, duration):
    if not 20 <= body.end - body.start <= 45 or body.end > duration:
        raise HTTPException(422, 'Choose a 20–45 second window within the source')
    if body.framing not in {'fit', 'crop', 'landscape', 'blur', 'dual'}:
        raise HTTPException(422, 'Unknown framing')
    if body.framing == 'dual' and body.dual_crop is None:
        raise HTTPException(422, 'Choose both facecam and gameplay crop areas')
    if body.caption_style not in {'highlight', 'classic', 'yellow', 'box', 'none', 'active', 'pop'}:
        raise HTTPException(422, 'Unknown caption style')
    if body.caption_position not in {'top','middle','lower_middle','bottom'}:
        raise HTTPException(422, 'Unknown caption position')
    previous_split = -1
    for index, segment in enumerate(body.crop_segments):
        if (index == 0 and segment.time != 0) or segment.time >= body.end-body.start or segment.time <= previous_split:
            raise HTTPException(422, 'Crop sections must start at zero and have ordered split points within the clip')
        previous_split = segment.time
    previous = 0
    for cue in body.captions:
        if cue.start < previous or cue.end <= cue.start or cue.end > body.end-body.start:
            raise HTTPException(422, 'Caption times must be ordered, non-overlapping, and within the clip')
        previous = cue.end

@app.post('/api/sources/{id}/clips')
def manual(id: str, body: Edit):
    with Session.begin() as db:
        source = get(db, Source, id)
        if source.status != 'ready':
            raise HTTPException(409, 'Wait for source analysis to finish')
        validate(body, source.duration)
        clip = Clip(id=uid(), source_id=id, **body.model_dump(exclude={'captions','dual_crop'}), dual_crop=body.dual_crop.model_dump() if body.dual_crop else {}, captions=[c.model_dump() for c in body.captions] or captions_for(source.words, body.start, body.end))
        db.add(clip)
        enqueue(db, 'preview', clip.id)
        return {'id': clip.id}

@app.put('/api/clips/{id}')
def edit(id: str, body: Edit):
    with Session.begin() as db:
        clip = get(db, Clip, id)
        if clip.status == 'rendering' or db.scalar(select(Job).where(Job.target == id, Job.status.in_(['running', 'queued']))):
            raise HTTPException(409, 'Wait for the current render to finish')
        if db.get(Publication, id):
            raise HTTPException(409, 'Remove this clip from the publishing queue before editing')
        validate(body, get(db, Source, clip.source_id).duration)
        for key, value in body.model_dump().items():
            setattr(clip, key, {} if key == 'dual_crop' and value is None else value)
        clip.status, clip.preview, clip.export = 'review', None, None
        cancel_rejection(db, id)
        enqueue(db, 'preview', id)
    return {'ok': True}

class Decision(BaseModel):
    approved: bool

@app.post('/api/clips/{id}/decision')
def decide(id: str, body: Decision):
    with Session.begin() as db:
        clip = get(db, Clip, id)
        if db.get(Publication, id):
            raise HTTPException(409, 'Remove this clip from the queue first')
        if not clip.preview or clip.status == 'rendering' or db.scalar(select(Job).where(Job.target == id, Job.status.in_(['running', 'queued']))):
            raise HTTPException(409, 'Wait for the preview to finish')
        if body.approved:
            clip.status = 'exported' if clip.export else 'approved'
            cancel_rejection(db, id)
        else:
            clip.status = 'rejected'
            if not db.get(Rejection, id):
                db.add(Rejection(clip_id=id, delete_after=datetime.now(timezone.utc) + timedelta(hours=24)))
    return {'ok': True}

class Batch(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=15)

class BulkDecision(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=1000)
    approved: bool

@app.post('/api/decisions')
def decisions(body: BulkDecision):
    with Session.begin() as db:
        clips = [get(db, Clip, id) for id in sorted(set(body.ids))]
        for clip in clips:
            if db.get(Publication, clip.id) or not clip.preview or clip.status == 'rendering' or db.scalar(select(Job).where(Job.target == clip.id, Job.status.in_(['running', 'queued']))):
                raise HTTPException(409, 'Select ready clips outside the publishing queue')
        for clip in clips:
            if body.approved:
                clip.status = 'exported' if clip.export else 'approved'
                cancel_rejection(db, clip.id)
            else:
                clip.status = 'rejected'
                if not db.get(Rejection, clip.id):
                    db.add(Rejection(clip_id=clip.id, delete_after=datetime.now(timezone.utc) + timedelta(hours=24)))
    return {'changed': len(clips)}

class FormatBatch(Batch):
    framing: str

@app.post('/api/formats')
def format_batch(body: FormatBatch):
    if body.framing not in {'fit', 'crop', 'landscape', 'blur'}:
        raise HTTPException(422, 'Unknown output format')
    with Session.begin() as db:
        clips = [get(db, Clip, id) for id in sorted(set(body.ids))]
        changed = [clip for clip in clips if clip.framing != body.framing]
        for clip in changed:
            if db.get(Publication, clip.id):
                raise HTTPException(409, 'Remove queued clips from the publishing plan before changing format')
            if clip.status == 'rendering' or db.scalar(select(Job).where(Job.target == clip.id, Job.status.in_(['running', 'queued']))):
                raise HTTPException(409, 'Wait for current renders to finish before changing format')
        for clip in changed:
            clip.framing = body.framing
            if body.framing == 'crop':
                clip.caption_position = 'lower_middle'
            clip.status, clip.preview, clip.export = 'review', None, None
            cancel_rejection(db, clip.id)
            enqueue(db, 'preview', clip.id)
    return {'ok': True, 'changed': len(changed)}

@app.post('/api/export')
def export(body: Batch):
    with Session.begin() as db:
        clips = [get(db, Clip, id) for id in sorted(set(body.ids))]
        if any(c.status not in {'approved', 'exported', 'rendering'} for c in clips):
            raise HTTPException(409, 'Only approved clips can be exported')
        for clip in clips:
            ensure_export(db, clip)
    return {'ok': True}

class Schedule(Batch):
    starts_at: datetime
    interval_hours: int = Field(ge=1, le=168, default=24)
    delivery: str = 'studio'
    youtube_mode: str = 'scheduled'
    titles: dict[str, str] = Field(default_factory=dict, max_length=15)

def rename_upload_clip(db, clip, title):
    title = title.strip()
    if not title or len(title) > 100 or '<' in title or '>' in title:
        raise HTTPException(422, 'Use a title of 1–100 characters without < or >')
    if title == clip.title:
        return
    upload = db.scalar(select(YouTubeUpload).where(YouTubeUpload.clip_id == clip.id).with_for_update())
    if upload and (upload.youtube_id or upload.session_cipher or upload.status == 'uploading'):
        raise HTTPException(409, 'This upload has started. Rename the video in YouTube Studio.')
    publication = db.get(Publication, clip.id)
    if publication and publication.youtube_id:
        raise HTTPException(409, 'Rename the uploaded video in YouTube Studio.')
    clip.title = title

class ClipTitle(BaseModel):
    title: str = Field(min_length=1, max_length=100)

@app.put('/api/clips/{id}/title')
def rename_clip(id: str, body: ClipTitle, request: Request):
    youtube_origin(request)
    with Session.begin() as db:
        clip = get(db, Clip, id)
        rename_upload_clip(db, clip, body.title)
        return {'id': id, 'title': clip.title}

@app.post('/api/schedule')
def schedule(body: Schedule,request: Request):
    from datetime import timedelta
    if body.starts_at.tzinfo is None or body.starts_at <= datetime.now(timezone.utc):
        raise HTTPException(422, 'Choose a future time with a timezone')
    if body.delivery not in {'studio','youtube'} or body.youtube_mode not in {'scheduled','private'}:
        raise HTTPException(422,'Unknown publishing mode')
    if body.delivery=='youtube':
        youtube_origin(request)
    if body.delivery=='youtube' and not connection_status()['connected']:
        raise HTTPException(409,'Connect your YouTube channel before scheduling uploads')
    with Session.begin() as db:
        ordered = list(dict.fromkeys(body.ids))
        clips = {id:get(db,Clip,id) for id in sorted(ordered)}
        if set(body.titles) - set(ordered):
            raise HTTPException(422, 'Titles must belong to selected clips')
        for id in sorted(body.titles):
            rename_upload_clip(db, clips[id], body.titles[id])
        if any(clip.status not in {'approved','exported','rendering'} for clip in clips.values()):
            raise HTTPException(409, 'Approve every selected clip before scheduling')
        rendering = 0
        for index, id in enumerate(ordered):
            planned = body.starts_at + timedelta(hours=index * body.interval_hours)
            publication = db.get(Publication,id)
            if publication:
                existing = publication.scheduled_at
                if existing.tzinfo is None:
                    existing = existing.replace(tzinfo=timezone.utc)
                if existing != planned:
                    raise HTTPException(409, 'This clip already has a different schedule. Remove it from the queue to reschedule.')
            pending = ensure_export(db, clips[id])
            rendering += int(pending)
            if not publication:
                db.add(Publication(clip_id=id,scheduled_at=planned,status='rendering' if pending else 'studio_export'))
            if body.delivery=='youtube':
                db.flush()
                try:
                    queue_upload(db,id,body.youtube_mode)
                except ValueError as exc:
                    raise HTTPException(409,str(exc)) from exc
    return {'ok': True, 'mode': body.delivery, 'rendering': rendering}

class PublishBatch(Batch):
    mode: str = 'scheduled'

@app.post('/api/youtube/publish')
def publish_batch(request: Request,body: PublishBatch):
    youtube_origin(request)
    with Session.begin() as db:
        for id in sorted(set(body.ids)):
            clip=get(db,Clip,id)
            publication=get(db,Publication,id)
            if clip.status not in {'approved','rendering','exported'}:
                raise HTTPException(409,'Only approved clips can be uploaded')
            if body.mode=='scheduled':
                planned=publication.scheduled_at
                if planned.tzinfo is None:
                    planned=planned.replace(tzinfo=timezone.utc)
                if planned<=datetime.now(timezone.utc):
                    raise HTTPException(422,'Choose a future publication time before uploading')
            try:
                queue_upload(db,id,body.mode)
            except ValueError as exc:
                raise HTTPException(409,str(exc)) from exc
            ensure_export(db,clip)
    return {'ok':True}

@app.delete('/api/schedule/{id}')
def unschedule(id: str):
    with Session.begin() as db:
        get(db,Clip,id)
        upload=db.get(YouTubeUpload,id)
        if upload:
            if upload.youtube_id or upload.session_cipher or upload.status=='uploading':
                raise HTTPException(409,'This clip has started uploading. Manage its schedule in YouTube Studio; it will not be deleted remotely.')
            db.delete(upload)
            db.flush()
        db.delete(get(db, Publication, id))
    return {'ok': True}

@app.get('/api/media/{filename}')
def media(filename: str):
    if not re.fullmatch(r'[a-f0-9]{32}(?:-preview|-export)?\.mp4', filename):
        raise HTTPException(404)
    path = DATA / filename
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type='video/mp4')

@app.post('/api/bundle')
def bundle(body: Batch):
    with Session() as db:
        clips = [get(db, Clip, id) for id in dict.fromkeys(body.ids)]
        if any(c.status != 'exported' or not c.export or not (DATA / c.export).is_file() for c in clips):
            raise HTTPException(409, 'Wait for all selected exports to finish')
        ensure_space(DATA,sum((DATA/clip.export).stat().st_size for clip in clips))
        path = DATA / f'{uid()}.zip'
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_STORED) as archive:
            text = io.StringIO()
            writer = csv.writer(text)
            writer.writerow(['file', 'title', 'scheduled_at', 'status', 'youtube_video_id'])
            for clip in clips:
                archive.write(DATA / clip.export, f'{clip.id}.mp4')
                publication = db.get(Publication, clip.id)
                writer.writerow([f'{clip.id}.mp4', clip.title, publication.scheduled_at.isoformat() if publication else '', publication.status if publication else 'unscheduled', publication.youtube_id if publication else ''])
            archive.writestr('youtube-studio-schedule.csv', text.getvalue())
        from starlette.background import BackgroundTask
        return FileResponse(path, filename='vi-clips-approved.zip', background=BackgroundTask(path.unlink, missing_ok=True))
