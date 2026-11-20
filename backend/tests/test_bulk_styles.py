import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from support import scratch
from fastapi.testclient import TestClient
from db import Session, Source, Clip, Publication, Rejection, init
from main import app
from media import render, run
from datetime import datetime, timezone


class BulkStyleTests(unittest.TestCase):
    def test_bulk_rejection_is_atomic_and_preserves_deadline(self):
        init()
        source = uuid.uuid4().hex
        ids = [uuid.uuid4().hex for _ in range(2)]
        def cleanup():
            with Session.begin() as db:
                for id in ids:
                    for model in (Publication, Rejection, Clip):
                        record=db.get(model,id)
                        if record:
                            db.delete(record)
                            db.flush()
                record=db.get(Source,source)
                if record:
                    db.delete(record)
        self.addCleanup(cleanup)
        with Session.begin() as db:
            db.add(Source(id=source, name='Bulk test', duration=60, status='ready'))
            db.flush()
            db.add_all([Clip(id=id, source_id=source, title='Clip', start=0, end=25, preview='test.mp4') for id in ids])
        with TestClient(app) as client:
            with Session.begin() as db:
                db.add(Publication(clip_id=ids[1], scheduled_at=datetime.now(timezone.utc)))
            self.assertEqual(client.post('/api/decisions', json={'ids':ids,'approved':False}).status_code,409)
            with Session.begin() as db:
                self.assertEqual(db.get(Clip,ids[0]).status,'review')
                db.delete(db.get(Publication,ids[1]))
            self.assertEqual(client.post('/api/decisions', json={'ids':ids,'approved':False}).status_code,200)
            with Session() as db:
                deadline = db.get(Rejection,ids[0]).delete_after
            client.post('/api/decisions', json={'ids':ids,'approved':False})
            with Session() as db:
                self.assertEqual(db.get(Rejection,ids[0]).delete_after,deadline)
            for extra in [{'crop_x':2},{'caption_style':'invalid'}]:
                response=client.put('/api/clips/'+ids[0],json={'title':'Clip','start':0,'end':25,**extra})
                self.assertEqual(response.status_code,422)

    def test_real_crop_positions_and_caption_styles(self):
        with TemporaryDirectory() as folder:
            root=Path(folder)
            source=root/'source.mp4'
            run(['ffmpeg','-y','-v','error','-f','lavfi','-i','testsrc2=size=640x360:rate=10','-t','0.5','-c:v','libx264',str(source)])
            for i,style in enumerate(['highlight','classic','yellow','box','none']):
                clip=SimpleNamespace(start=0,end=.5,framing='crop',crop_x=i/4,crop_y=.5,caption_style=style,captions=[{'start':0,'end':.5,'text':'Caption test'}])
                output=root/f'clip{i}.mp4'
                render(source,clip,output)
                self.assertGreater(output.stat().st_size,1000)
                ass=output.with_suffix('.ass').read_text()
                self.assertEqual('Dialogue:' in ass,style!='none')
                self.assertEqual('BA73FF' in ass,style=='highlight')
