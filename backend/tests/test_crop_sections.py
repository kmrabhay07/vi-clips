import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from fastapi import HTTPException
from main import Edit, validate
from media import run, render


class CropSectionTests(unittest.TestCase):
    def test_render_changes_crop_at_split_relative_to_clip_start(self):
        with TemporaryDirectory() as folder:
            root=Path(folder)
            source=root/'source.mp4'
            run(['ffmpeg','-y','-v','error','-f','lavfi','-i','color=red:size=640x360:rate=10','-vf','drawbox=x=320:y=0:w=320:h=360:color=blue:t=fill','-t','3','-c:v','libx264',str(source)])
            clip=SimpleNamespace(start=.5,end=2.5,framing='crop',captions=[],crop_x=.5,crop_y=.5,crop_segments=[dict(time=0,x=0,y=.5),dict(time=1,x=1,y=.5)])
            output=root/'split.mp4'
            render(source,clip,output)
            colors=[]
            for time in (.5,1.5):
                colors.append(run(['ffmpeg','-v','error','-ss',str(time),'-i',str(output),'-frames:v','1','-vf','crop=2:2:180:100,scale=1:1','-pix_fmt','rgb24','-f','rawvideo','-']))
            self.assertGreater(colors[0][0],180)
            self.assertLess(colors[0][2],70)
            self.assertLess(colors[1][0],70)
            self.assertGreater(colors[1][2],180)

    def test_split_validation(self):
        good=[dict(time=0,x=0,y=.5),dict(time=10,x=1,y=.5)]
        validate(Edit(title='Clip',start=100,end=125,crop_segments=good),150)
        for segments in [[dict(time=1,x=.5,y=.5)],good[::-1],good+[dict(time=10,x=.2,y=.5)],good+[dict(time=25,x=.2,y=.5)]]:
            with self.assertRaises(HTTPException):
                validate(Edit(title='Clip',start=100,end=125,crop_segments=segments),150)
        with self.assertRaises(ValueError):
            Edit(title='Clip',start=100,end=125,crop_segments=[dict(time=0,x=2,y=.5)])
