"""Reuse analysis signals only while the original recording is unchanged."""
import math
import numpy as np

def fingerprint(path, duration):
    stat=path.stat()
    return dict(size=stat.st_size,mtime_ns=stat.st_mtime_ns,duration=duration)

def saved_signals(path, duration, motion, audio):
    return dict(version=1,**fingerprint(path,duration),motion=motion.tolist(),audio=audio.tolist())

def cached_signals(document, path, duration):
    if not isinstance(document,dict) or not document:
        return None
    try:
        current=fingerprint(path,duration)
        if document.get('version')!=1 or any(document.get(key)!=value for key,value in current.items()):
            return None
        motion=np.asarray(document['motion'],dtype=float)
        audio=np.asarray(document['audio'],dtype=float)
        if (motion.ndim!=1 or audio.ndim!=1 or not 0<len(motion)<=math.ceil(duration)+1
                or len(motion)!=len(audio) or not np.isfinite(motion).all() or not np.isfinite(audio).all()):
            return None
        return motion,audio
    except (OSError,KeyError,TypeError,ValueError):
        return None
