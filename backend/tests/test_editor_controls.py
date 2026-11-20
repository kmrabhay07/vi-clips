import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import numpy as np
from media import render, run, probe
from main import Edit, validate
from fastapi import HTTPException


class EditorControlTests(unittest.TestCase):
    def test_audio_gain_mute_and_caption_size_position_render(self):
        with TemporaryDirectory() as folder:
            root=Path(folder)
            source=root/'source.mp4'
            run(['ffmpeg','-y','-v','error','-f','lavfi','-i','color=blue:size=320x180:rate=10','-f','lavfi','-i','sine=frequency=440:sample_rate=44100','-t','1','-c:v','libx264','-c:a','aac',str(source)])
            levels=[]
            for gain,muted in [(1,False),(.25,False),(1,True)]:
                clip=SimpleNamespace(start=0,end=1,framing='crop',captions=[dict(start=0,end=1,text='Editor test')],caption_scale=1.5,caption_position='lower_middle' if gain==.25 else 'top',volume=gain,audio_muted=muted)
                output=root/f'{gain}-{muted}.mp4'
                render(source,clip,output)
                self.assertEqual(probe(output)[1],not muted)
                self.assertIn('Default,DejaVu Sans,90,',output.with_suffix('.ass').read_text())
                self.assertIn(',480,1' if gain==.25 else ',1780,1',output.with_suffix('.ass').read_text())
                if not muted:
                    pcm=run(['ffmpeg','-v','error','-i',str(output),'-f','f32le','-ac','1','-ar','16000','-'])
                    samples=np.frombuffer(pcm,dtype=np.float32)
                    levels.append(float(np.sqrt(np.mean(samples**2))))
            self.assertAlmostEqual(levels[1]/levels[0],.25,delta=.04)

    def test_editor_settings_validation(self):
        validate(Edit(title='Clip',start=0,end=25,volume=2,caption_scale=1.6,caption_position='middle'),60)
        validate(Edit(title='Clip',start=0,end=25,caption_position='lower_middle'),60)
        with self.assertRaises(HTTPException):
            validate(Edit(title='Clip',start=0,end=25,caption_position='invalid'),60)
        for extra in [{'volume':3},{'caption_scale':0},{'volume':float('nan')}]:
            with self.assertRaises(ValueError):
                Edit(title='Clip',start=0,end=25,**extra)
