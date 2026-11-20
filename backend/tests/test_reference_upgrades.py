import unittest
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from pathlib import Path
import numpy as np
from caption_events import word_events
from media import choose, render, run


class ReferenceUpgradeTests(unittest.TestCase):
    def test_word_alignment_and_edited_text_fallback(self):
        words=[dict(start=10+i*.3,end=10+i*.3+.25,text=text) for i,text in enumerate(['That','was','so','close'])]
        cues=[dict(start=0,end=1.3,text='That was so close')]
        events=word_events(cues,words,10)
        self.assertEqual([e['active_index'] for e in events],[0,1,2,0])
        self.assertEqual(events[-1]['tokens'],['close'])
        self.assertTrue(all(a['end']<=b['start'] for a,b in zip(events,events[1:])))
        fallback=word_events([dict(start=0,end=1.3,text='My edited sentence')],words,10)
        self.assertIsNone(fallback[0]['active_index'])
        self.assertEqual(fallback[0]['tokens'],['My','edited','sentence'])

    def test_short_peak_gets_lead_in_and_final_clips_do_not_overlap(self):
        motion=np.zeros(120);audio=np.zeros(120)
        motion[40:44]=1;audio[40:44]=1
        picks=choose(120,motion,audio,[],'',limit=3)
        self.assertTrue(picks)
        self.assertTrue(34<=picks[0]['start']<=38,picks)
        self.assertLess(picks[0]['start'],40)
        self.assertTrue(all(a['end']<=b['start'] or b['end']<=a['start'] for i,a in enumerate(picks) for b in picks[i+1:]))

    def test_real_blurred_fit_pop_caption_render(self):
        with TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source.mp4';output=root/'blur.mp4'
            run(['ffmpeg','-y','-v','error','-f','lavfi','-i','testsrc2=size=640x360:rate=10','-t','1','-c:v','libx264',str(source)])
            words=[dict(start=0,end=.3,text='Hello'),dict(start=.4,end=.8,text='world')]
            clip=SimpleNamespace(start=0,end=1,framing='blur',caption_style='pop',captions=[dict(start=0,end=1,text='Hello world')])
            render(source,clip,output,words=words)
            self.assertGreater(output.stat().st_size,1000)
            ass=output.with_suffix('.ass').read_text()
            self.assertEqual(ass.count('Dialogue:'),2)
            self.assertIn(r'\t(0,100,\fscx100\fscy100)',ass)
            dimensions=run(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=width,height','-of','csv=p=0',str(output)]).decode().strip()
            self.assertEqual(dimensions,'360,640')
            # Blurred fit fills the letterbox instead of leaving our solid padding color.
            pixel=run(['ffmpeg','-v','error','-i',str(output),'-frames:v','1','-vf','crop=2:2:100:30,scale=1:1','-f','rawvideo','-pix_fmt','rgb24','-'])
            self.assertGreater(sum(pixel),80)
