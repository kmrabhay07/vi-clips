import logging
import os
import signal
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass
from functools import lru_cache
from threading import Event, Lock
from sqlalchemy import select, text, case
from db import DATA, Session, engine, Source, Clip, Job, SourceProgress, Publication, init
from media import probe, motion_signal, audio_signal, align_audio, choose, captions_for, render
from settings import performance
from retention import cleanup_rejected
from published_cleanup import cleanup_published
from youtube import download_video
from youtube_publish import claim_upload,upload_one,recover_uploads
from analysis_cache import cached_signals,saved_signals,fingerprint
from transcription import transcribe_chunks

log = logging.getLogger('vi-clips.worker')

@lru_cache(maxsize=1)
def whisper_model():
    # Only one analysis job runs at a time. Reuse its model across recordings.
    from faster_whisper import WhisperModel
    return WhisperModel(os.getenv('WHISPER_MODEL', 'tiny'), device='cpu',
                        compute_type='int8', cpu_threads=performance.whisper_threads,
                        num_workers=1)

def audio_and_words(path, duration, has_audio, cached_words=None):
    wav = path.with_suffix('.wav')
    try:
        audio, wav = audio_signal(path, duration, has_audio)
        words = list(cached_words) if cached_words is not None else []
        if has_audio and cached_words is None:
            identity=fingerprint(path,duration) if path.is_file() else None
            checkpoint=None
            if identity and len(path.stem)==32:
                with Session() as progress_db:
                    source=progress_db.get(Source,path.stem)
                    saved=(source.analysis_cache or {}).get('speech',{}) if source else {}
                    if saved.get('fingerprint')==identity:
                        checkpoint=saved
            def save(checkpoint,completed,total):
                if not identity or len(path.stem)!=32:
                    return
                with Session.begin() as progress_db:
                    source=progress_db.get(Source,path.stem)
                    if source is None:
                        return
                    source.analysis_cache={**(source.analysis_cache or {}),'speech':{**checkpoint,'fingerprint':identity}}
                    progress=progress_db.get(SourceProgress,path.stem)
                    if progress is None:
                        progress=SourceProgress(source_id=path.stem);progress_db.add(progress)
                    progress.details={'stage':'transcribing','completed':completed,'total':total,'unit':'seconds'}
                try:
                    from early_clips import create_early_clips
                    create_early_clips(path.stem)
                except Exception:
                    log.exception('Early highlights deferred; source analysis continues')
            words=transcribe_chunks(whisper_model(),wav,performance.whisper_beam_size,checkpoint,save)
        return audio, words
    finally:
        wav.unlink(missing_ok=True)

def analyze(db, source, regenerate=False):
    path = DATA / f'{source.id}.mp4'
    duration, has_audio = probe(path)
    source.status = 'analyzing'
    db.commit()
    # Video scanning overlaps audio extraction and transcription. No ORM objects
    # cross into these threads; only the owning job thread uses the session.
    cached=cached_signals(getattr(source,'analysis_cache',{}),path,duration) if regenerate else None
    if cached is not None:
        motion,audio=cached
        words=source.words
        log.info('Reusing cached analysis for %s',source.id)
    else:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix='analysis') as pool:
            visual = pool.submit(motion_signal, path)
            spoken = pool.submit(audio_and_words, path, duration, has_audio, source.words if regenerate else None)
            motion = visual.result()
            audio, words = spoken.result()
        audio = align_audio(audio, len(motion))
        if path.is_file():
            source.analysis_cache=saved_signals(path,duration,motion,audio)
    source.words = words
    source.duration = duration
    excluded = [(clip.start,clip.end) for clip in db.scalars(select(Clip).where(Clip.source_id == source.id))]
    candidates = choose(duration, motion, audio, words, source.prompt, exclude=excluded)
    log.info('Selected %d new highlights for %s; excluding %d existing windows',len(candidates),source.id,len(excluded))
    for i, candidate in enumerate(candidates):
        clip = Clip(id=uuid.uuid4().hex, source_id=source.id,
                    title=f'Gameplay highlight {i+1}', framing='dual' if source.dual_crop else 'crop',
                    dual_crop=source.dual_crop or {},
                    captions=captions_for(words, candidate['start'], candidate['end']),
                    **candidate)
        db.add(clip)
        db.flush()
        db.add(Job(id=uuid.uuid4().hex, kind='preview', target=clip.id))
    source.status = 'ready'
    source.error = None

def process(db, job):
    if job.kind == 'import_youtube':
        source = db.get(Source, job.target)
        source.status = 'downloading'
        db.commit()
        last_update=[0,None]
        update_lock=Lock()
        def update(details):
            with update_lock:
                now=time.monotonic()
                stage=details['stage']
                if stage==last_update[1] and now-last_update[0]<1:
                    return
                with Session.begin() as progress_db:
                    item=progress_db.get(SourceProgress,source.id)
                    if item is None:
                        item=SourceProgress(source_id=source.id)
                        progress_db.add(item)
                    item.details=details
                    current=progress_db.get(Source,source.id)
                    current.status=stage
                    if details.get('title'):
                        current.name=details['title'][:250]
                    if details.get('duration'):
                        current.duration=details['duration']
                last_update[:]=[now,stage]
        source.name, source.duration = download_video(source.youtube_id, DATA / f'{source.id}.mp4', on_update=update, **({'use_cookies': True} if source.use_browser_cookies else {}))
        source.status, source.error = ('awaiting_layout' if source.layout_pending else 'queued'), None
        progress=db.get(SourceProgress,source.id)
        if progress:
            progress.details={'stage':'downloaded','completed':(DATA/f'{source.id}.mp4').stat().st_size,'total':(DATA/f'{source.id}.mp4').stat().st_size}
        if not source.layout_pending:
            db.add(Job(id=uuid.uuid4().hex, kind='analyze', target=source.id))
    elif job.kind in {'analyze', 'highlights'}:
        analyze(db, db.get(Source, job.target), regenerate=job.kind == 'highlights')
    elif job.kind in {'preview', 'export'}:
        clip = db.get(Clip, job.target)
        full = job.kind == 'export'
        filename = f"{clip.id}-{'export' if full else 'preview'}.mp4"
        options={'words':db.get(Source,clip.source_id).words} if clip.caption_style in {'active','pop'} else {}
        render(DATA / f'{clip.source_id}.mp4', clip, DATA / filename, full, **options)
        if full:
            clip.export, clip.status = filename, 'exported'
            publication = db.get(Publication,clip.id)
            if publication:
                publication.status = 'studio_export'
        else:
            clip.preview = filename
    else:
        raise ValueError(f'Unknown job kind: {job.kind}')

@dataclass(frozen=True)
class ClaimedJob:
    id: str
    kind: str
    target: str

def claim_job(active):
    """Atomically claim a job; never run two jobs against the same target."""
    allowed = ['preview', 'export']
    if not any(job.kind in {'analyze', 'import_youtube', 'highlights'} for job in active):
        allowed.extend(['analyze', 'import_youtube', 'highlights'])
    with Session.begin() as db:
        query = select(Job).where(Job.status == 'queued', Job.kind.in_(allowed))
        if active:
            query = query.where(Job.target.not_in([job.target for job in active]))
        priority = case((Job.kind.in_(['analyze', 'import_youtube', 'highlights']), 0), (Job.kind == 'preview', 1), else_=2)
        job = db.scalar(query.order_by(priority, Job.created, Job.id)
                        .limit(1).with_for_update(skip_locked=True))
        if job is None:
            return None
        job.status, job.error = 'running', None
        return ClaimedJob(job.id, job.kind, job.target)

def recover_interrupted():
    with Session.begin() as db:
        for job in db.scalars(select(Job).where(Job.status == 'running')):
            job.status = 'queued'

def run_job(claimed):
    # Each thread gets its own session and ORM objects.
    started = time.monotonic()
    log.info('Starting %s %s', claimed.kind, claimed.id)
    with Session() as db:
        try:
            job = db.get(Job, claimed.id)
            process(db, job)
            job.status = 'done'
            db.commit()
            log.info('Finished %s %s in %.2fs', claimed.kind, claimed.id,
                     time.monotonic() - started)
        except Exception as exc:
            db.rollback()
            job = db.get(Job, claimed.id)
            job.status, job.error = 'failed', str(exc)[-2500:]
            if job.kind in {'analyze', 'import_youtube', 'highlights'}:
                source = db.get(Source, job.target)
                source.status, source.error = 'failed', job.error
            elif job.kind == 'export':
                db.get(Clip, job.target).status = 'approved'
                publication = db.get(Publication,job.target)
                if publication:
                    publication.status = 'render_failed'
            db.commit()
            log.exception('Failed %s %s', claimed.kind, claimed.id)

def dispatch(pool, active, limit):
    while len(active) < limit:
        claimed = claim_job(list(active.values()))
        if claimed is None:
            break
        active[pool.submit(run_job, claimed)] = claimed

def run_cleanup():
    try:
        cleanup_rejected()
        cleanup_published()
    except Exception:
        log.exception('Storage cleanup will retry in one minute')

def serve(stop, concurrency):
    active = {}
    next_cleanup = 0
    upload_future = None
    cleanup_future = None
    next_upload_check = 0
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix='clip-job') as pool, ThreadPoolExecutor(max_workers=1,thread_name_prefix='youtube-upload') as upload_pool, ThreadPoolExecutor(max_workers=1,thread_name_prefix='storage-cleanup') as cleanup_pool:
        while not stop.is_set():
            if upload_future is not None and upload_future.done():
                try:
                    upload_future.result()
                except Exception:
                    log.error('YouTube uploader could not persist its state; restarting is required')
                    raise
                upload_future = None
                next_upload_check = 0
            if upload_future is None and time.monotonic() >= next_upload_check:
                upload_id=claim_upload()
                next_upload_check=time.monotonic()+2
                if upload_id:
                    upload_future=upload_pool.submit(upload_one,upload_id,stop)
            if time.monotonic() >= next_cleanup and (cleanup_future is None or cleanup_future.done()):
                cleanup_future = cleanup_pool.submit(run_cleanup)
                next_cleanup = time.monotonic() + 60
            dispatch(pool, active, concurrency)
            if not active:
                stop.wait(.5)
                continue
            finished, _ = wait(active, timeout=.5, return_when=FIRST_COMPLETED)
            for future in finished:
                claimed = active.pop(future)
                try:
                    future.result()
                except Exception:
                    log.exception('Could not persist job %s', claimed.id)
                    raise
        log.info('Draining %d active jobs before shutdown', len(active))
    # Executor shutdown waits for jobs before the coordinator lock releases.

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    init()
    stop = Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    # One coordinator owns restart recovery and dispatches concurrent jobs.
    with engine.connect() as lock:
        if engine.dialect.name == 'postgresql':
            if not lock.execute(text('SELECT pg_try_advisory_lock(621901)')).scalar():
                raise RuntimeError('Another worker coordinator is already active')
            lock.commit()
        recover_interrupted()
        recover_uploads()
        log.info('Parallel worker online: %s', performance.public())
        serve(stop, performance.concurrency)

if __name__ == '__main__':
    main()
