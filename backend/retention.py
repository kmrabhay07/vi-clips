import logging
import re
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, delete
from db import DATA, Session, Clip, Rejection, Publication, Job

log = logging.getLogger('vi-clips.retention')

def delete_rejected_clip(db, clip):
    if clip.status != 'rejected' or db.get(Publication, clip.id) or db.scalar(select(Job).where(
        Job.target == clip.id, Job.status.in_(['queued', 'running']))):
        return False
    if not re.fullmatch(r'[a-f0-9]{32}', clip.id):
        raise ValueError('Refusing to delete files for an invalid clip ID')
    for suffix in ('-preview.mp4', '-preview.ass', '-export.mp4', '-export.ass'):
        path = DATA / f'{clip.id}{suffix}'
        if path.is_symlink() or path.resolve().parent != DATA:
            raise ValueError('Clip media path escaped storage')
        path.unlink(missing_ok=True)
    db.execute(delete(Job).where(Job.target == clip.id))
    cancel_rejection(db, clip.id)
    db.flush()
    db.delete(clip)
    return True

def empty_rejected():
    deleted=skipped=0
    with Session.begin() as db:
        clips=list(db.scalars(select(Clip).where(Clip.status=='rejected').with_for_update(skip_locked=True)))
        for clip in clips:
            if delete_rejected_clip(db,clip):
                deleted+=1
            else:
                skipped+=1
    return {'deleted':deleted,'skipped':skipped}

def cancel_rejection(db, clip_id):
    item = db.get(Rejection, clip_id)
    if item:
        db.delete(item)

def cleanup_rejected(now=None):
    now = now or datetime.now(timezone.utc)
    deleted = 0
    with Session.begin() as db:
        # Legacy rejected clips get a full grace period starting at deployment.
        legacy = db.scalars(select(Clip).where(Clip.status == 'rejected',
            ~select(Rejection).where(Rejection.clip_id == Clip.id).exists())
            .with_for_update(skip_locked=True))
        for clip in legacy:
            db.add(Rejection(clip_id=clip.id, delete_after=now + timedelta(hours=24)))
        db.flush()
        clips = list(db.scalars(select(Clip).join(Rejection, Rejection.clip_id == Clip.id)
            .where(Clip.status == 'rejected', Rejection.delete_after <= now)
            .with_for_update(of=Clip, skip_locked=True)))
        for clip in clips:
            deleted += int(delete_rejected_clip(db,clip))
    if deleted:
        log.info('Deleted %d rejected clips after their 24-hour grace period', deleted)
    return deleted
