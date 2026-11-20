import uuid
import unittest
import wave
import numpy as np
from sqlalchemy import select,delete
from support import scratch
from db import DATA,Session,Source,Clip,Job,init
from analysis_cache import fingerprint
from early_clips import create_early_clips

class EarlyClipTests(unittest.TestCase):
    def test_review_clips_appear_before_analysis_finishes_without_duplicates(self):
        init();id=uuid.uuid4().hex;path=DATA/f'{id}.mp4';wav=path.with_suffix('.wav')
        path.write_bytes(b'recording')
        with wave.open(str(wav),'wb') as audio:
            audio.setparams((1,2,1000,0,'NONE','not compressed'))
            audio.writeframes(np.repeat(np.linspace(1000,30000,120),1000).astype(np.int16).tobytes())
        words=[dict(start=float(i),end=i+.5,text='hello!') for i in range(120)]
        try:
            with Session.begin() as db:
                db.add(Source(id=id,name='Long recording',status='analyzing',duration=120,prompt='hello',
                    analysis_cache={'speech':{'completed_frames':120000,'words':words,'fingerprint':fingerprint(path,120)}}))
            result=create_early_clips(id)
            self.assertGreater(result['created'],0);self.assertLessEqual(result['created'],3)
            self.assertTrue(create_early_clips(id)['existing'])
            with Session() as db:
                clips=list(db.scalars(select(Clip).where(Clip.source_id==id)))
                self.assertEqual(len(clips),result['created'])
                self.assertEqual(db.get(Source,id).status,'analyzing')
                for clip in clips:
                    self.assertTrue(20<=clip.end-clip.start<=45)
                    self.assertLessEqual(clip.end,120)
                    self.assertTrue(clip.captions)
                    self.assertEqual(db.scalar(select(Job.kind).where(Job.target==clip.id)),'preview')
        finally:
            with Session.begin() as db:
                ids=list(db.scalars(select(Clip.id).where(Clip.source_id==id)))
                db.execute(delete(Job).where(Job.target.in_(ids)))
                db.execute(delete(Clip).where(Clip.source_id==id))
                db.execute(delete(Source).where(Source.id==id))
            path.unlink(missing_ok=True);wav.unlink(missing_ok=True)
