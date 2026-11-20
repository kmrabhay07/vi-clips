import unittest
import uuid
from support import scratch
from fastapi.testclient import TestClient
from db import Session, Source, init
from main import app
from media import retime_captions


class CaptionRetimeTests(unittest.TestCase):
    def test_trim_shift_and_preserve_edits(self):
        cues=[dict(start=0,end=2,text='first'),dict(start=8,end=12,text='edited wording'),dict(start=20,end=25,text='last')]
        result=retime_captions(cues,100,130,110,123,[])
        self.assertEqual(result,[dict(start=0,end=2,text='edited wording'),dict(start=10,end=13,text='last')])

    def test_extension_uses_transcript_and_normalizes_overlaps(self):
        words=[dict(start=95,end=97,text='Before'),dict(start=132,end=134,text='After')]
        cues=[dict(start=0,end=4,text='my correction'),dict(start=3,end=6,text='overlap')]
        result=retime_captions(cues,100,130,90,140,words)
        self.assertEqual([c['text'] for c in result],['Before','my correction','overlap','After'])
        self.assertEqual(result[1]['start'],10)
        self.assertEqual(result[2]['start'],14)
        self.assertEqual(result[-1]['start'],42)
        self.assertTrue(all(c['end']<=50 for c in result))

    def test_api_returns_relative_captions_and_saved_edit_validates(self):
        init()
        id=uuid.uuid4().hex
        with Session.begin() as db:
            db.add(Source(id=id,name='Timing fixture',status='ready',duration=90,words=[dict(start=42,end=44,text='New speech')]))
        with TestClient(app) as client:
            body=dict(old_start=10,old_end=40,start=20,end=45,captions=[dict(start=8,end=12,text='Keep correction'),dict(start=25,end=30,text='Tail')])
            response=client.post(f'/api/sources/{id}/retime-captions',json=body)
            self.assertEqual(response.status_code,200,response.text)
            cues=response.json()['captions']
            self.assertEqual(cues[0],dict(start=0,end=2,text='Keep correction'))
            self.assertEqual(cues[-1],dict(start=22,end=24,text='New speech'))
            from main import Edit,validate
            validate(Edit(title='Clip',start=20,end=45,captions=cues),90)
            body['end']=91
            self.assertEqual(client.post(f'/api/sources/{id}/retime-captions',json=body).status_code,422)
        with Session.begin() as db:
            db.delete(db.get(Source,id))
