"""Make a few reviewable audio highlights available during long-source analysis."""
import math
import uuid
import wave
import numpy as np
from sqlalchemy import select
from db import DATA,Session,Source,Clip,Job
from media import choose,captions_for
from analysis_cache import fingerprint

def create_early_clips(source_id):
    with Session.begin() as db:
        source=db.scalar(select(Source).where(Source.id==source_id).with_for_update())
        if source is None:
            raise ValueError('Source not found')
        if db.scalar(select(Clip.id).where(Clip.source_id==source_id)):
            return {'created':0,'existing':True}
        if source.status!='analyzing':
            return {'created':0,'waiting':True}
        speech=(source.analysis_cache or {}).get('speech',{})
        path=DATA/f'{source.id}.mp4';wav=path.with_suffix('.wav')
        if not wav.is_file() or speech.get('fingerprint')!=fingerprint(path,source.duration):
            return {'created':0,'waiting':True}
        with wave.open(str(wav),'rb') as audio:
            rate=audio.getframerate()
            completed=min(speech.get('completed_frames',0)/rate,source.duration)
            if completed<60 or audio.getnchannels()!=1 or audio.getsampwidth()!=2:
                return {'created':0,'waiting':True}
            offset=max(0,completed-1800)
            duration=completed-offset
            levels=np.zeros(math.ceil(duration))
            audio.setpos(round(offset*rate))
            for index in range(len(levels)):
                samples=np.frombuffer(audio.readframes(min(rate,round((duration-index)*rate))),dtype=np.int16).astype(float)
                if len(samples):levels[index]=np.sqrt(np.mean(samples**2))
        words=[dict(start=w['start']-offset,end=w['end']-offset,text=w['text'])
               for w in speech.get('words',[]) if w['end']>offset and w['start']<completed]
        candidates=choose(duration,np.zeros(len(levels)),levels,words,source.prompt,limit=3)
        for index,candidate in enumerate(candidates):
            start,end=candidate['start']+offset,candidate['end']+offset
            clip=Clip(id=uuid.uuid4().hex,source_id=source_id,title=f'Early highlight {index+1}',
                      start=start,end=end,score=candidate['score'],
                      reason='Early audio highlight from transcribed footage. Full-video analysis continues.',
                      captions=captions_for(speech.get('words',[]),start,end),
                      framing='dual' if source.dual_crop else 'crop',dual_crop=source.dual_crop or {})
            db.add(clip);db.flush()
            db.add(Job(id=uuid.uuid4().hex,kind='preview',target=clip.id))
        return {'created':len(candidates)}
