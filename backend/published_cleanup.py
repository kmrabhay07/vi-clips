"""Opt-in disk cleanup after fresh YouTube confirmation; retain a small receipt."""
import logging
import re
import shutil
from datetime import datetime, timezone
from sqlalchemy import select, delete
from db import DATA, Session, CleanupPolicy, PublishedHistory, Source, Clip, Job, Publication, YouTubeUpload, Rejection, SourceProgress

log=logging.getLogger('vi-clips.cleanup')

def policy_status():
    with Session() as db:
        policy=db.get(CleanupPolicy,1)
        return {'clips':bool(policy and policy.clips),'sources':bool(policy and policy.sources)}

def safe_file(name):
    path=DATA/name
    if path.is_symlink() or path.resolve().parent!=DATA:
        raise ValueError('Unsafe cleanup path')
    return path

def remove_confirmed_clip(id):
    with Session.begin() as db:
        policy=db.get(CleanupPolicy,1)
        if not policy or not policy.clips:
            return False
        candidate=db.get(Clip,id)
        if not candidate:
            return False
        source=db.scalar(select(Source).where(Source.id==candidate.source_id).with_for_update())
        clip=db.scalar(select(Clip).where(Clip.id==id).with_for_update())
        item=db.scalar(select(YouTubeUpload).where(YouTubeUpload.clip_id==id).with_for_update())
        publication=db.get(Publication,id)
        if not source or not clip or not item or not publication or item.status!='published' or not item.youtube_id:
            return False
        if item.remote.get('privacyStatus')!='public' or item.remote.get('uploadStatus')!='processed':
            return False
        if db.scalar(select(Job).where(Job.target.in_([id,source.id]),Job.status.in_(['queued','running']))):
            return False
        if not re.fullmatch(r'[a-f0-9]{32}',id):
            raise ValueError('Invalid clip ID')
        paths=[safe_file(id+suffix) for suffix in ('-preview.mp4','-preview.ass','-export.mp4','-export.ass')]
        if not db.get(PublishedHistory,id):
            db.add(PublishedHistory(clip_id=id,source_id=source.id,title=clip.title,youtube_id=item.youtube_id,scheduled_at=publication.scheduled_at))
        for path in paths:
            path.unlink(missing_ok=True)
        for model,column in [(Job,Job.target),(Rejection,Rejection.clip_id),(YouTubeUpload,YouTubeUpload.clip_id),(Publication,Publication.clip_id)]:
            db.execute(delete(model).where(column==id))
        db.delete(clip)
    return True

def remove_unused_sources():
    removed=0
    with Session.begin() as db:
        policy=db.get(CleanupPolicy,1)
        if not policy or not policy.clips or not policy.sources:
            return 0
        ids=list(db.scalars(select(PublishedHistory.source_id).distinct()))
        for id in ids:
            source=db.scalar(select(Source).where(Source.id==id).with_for_update())
            if not source or db.scalar(select(Clip).where(Clip.source_id==id)) or db.scalar(select(Job).where(Job.target==id,Job.status.in_(['queued','running']))):
                continue
            if not re.fullmatch(r'[a-f0-9]{32}',id):
                continue
            paths=[safe_file(id+suffix) for suffix in ('.mp4','.wav')]
            cache=None
            if source.youtube_id and re.fullmatch(r'[A-Za-z0-9_-]{11}',source.youtube_id):
                parent=DATA/'youtube-cache';cache=parent/source.youtube_id
                if parent.is_symlink() or cache.is_symlink() or cache.resolve().parent!=parent.resolve() or (cache.exists() and any(p.is_symlink() for p in cache.rglob('*'))):
                    raise ValueError('Unsafe source cache')
            for path in paths:
                path.unlink(missing_ok=True)
            if cache and cache.exists():
                shutil.rmtree(cache)
            db.execute(delete(SourceProgress).where(SourceProgress.source_id==id))
            db.execute(delete(Job).where(Job.target==id))
            db.delete(source);removed+=1
    return removed

def cleanup_published():
    if not policy_status()['clips']:
        return {'clips_deleted':0,'sources_deleted':0}
    from youtube_publish import refresh_remote
    now=datetime.now(timezone.utc)
    with Session() as db:
        candidates=[]
        for item,pub in db.execute(select(YouTubeUpload,Publication).join(Publication,Publication.clip_id==YouTubeUpload.clip_id).where(YouTubeUpload.youtube_id.is_not(None),YouTubeUpload.status.in_(['scheduled','uploaded_check_schedule','published']))):
            scheduled=pub.scheduled_at
            if scheduled.tzinfo is None:
                scheduled=scheduled.replace(tzinfo=timezone.utc)
            if scheduled<=now:
                candidates.append((item.remote.get('cleanup_checked_at',0),item.clip_id))
    removed=0
    for _,id in sorted(candidates)[:3]:
        try:
            refresh_remote(id)
            with Session.begin() as db:
                item=db.get(YouTubeUpload,id)
                if item:
                    item.remote={**item.remote,'cleanup_checked_at':int(now.timestamp())}
            removed+=int(remove_confirmed_clip(id))
        except Exception:
            log.warning('Publication verification/cleanup deferred for clip %s',id)
            with Session.begin() as db:
                item=db.get(YouTubeUpload,id)
                if item:
                    item.remote={**item.remote,'cleanup_checked_at':int(now.timestamp())}
    return {'clips_deleted':removed,'sources_deleted':remove_unused_sources()}
