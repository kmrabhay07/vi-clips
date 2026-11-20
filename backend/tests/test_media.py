import unittest
import wave
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch
import numpy as np
from media import choose, captions_for, render, ass_time, WATERMARK, run

class MediaTests(unittest.TestCase):
    def test_whisper_decodes_real_audio_with_installed_pyav(self):
        # Exercise the exact upstream av.open call; importing Whisper or mocking
        # transcribe does not detect the PyAV 19 metadata_errors incompatibility.
        from faster_whisper.audio import decode_audio
        with TemporaryDirectory() as folder:
            path = Path(folder)/'speech.wav'
            samples = (np.sin(np.arange(16000)*2*np.pi*440/16000)*4000).astype(np.int16)
            with wave.open(str(path), 'wb') as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(samples.tobytes())
            decoded = decode_audio(str(path), sampling_rate=16000)
            self.assertEqual(decoded.dtype, np.float32)
            self.assertEqual(len(decoded), 16000)
            self.assertTrue(np.isfinite(decoded).all())
            self.assertGreater(float(np.max(np.abs(decoded))), .1)

    def test_silent_static_source_has_no_invented_highlights(self):
        self.assertEqual(choose(90, np.zeros(90), np.zeros(90), [], 'chase'), [])

    def test_selected_windows_are_distinct_and_capped(self):
        picks = choose(1200, np.ones(1200), np.ones(1200), [], '', 15)
        self.assertEqual(len(picks), 15)
        for i, a in enumerate(picks):
            self.assertTrue(20 <= a['end'] - a['start'] <= 45)
            for b in picks[i+1:]:
                self.assertTrue(a['end'] <= b['start'] or a['start'] >= b['end'])

    def test_short_sources_return_no_windows(self):
        self.assertEqual(choose(19, np.ones(19), np.ones(19), [], ''), [])

    def test_fresh_highlights_exclude_previous_windows(self):
        previous=[(0,30),(60,90)]
        picks=choose(180,np.ones(180),np.ones(180),[],'',exclude=previous)
        self.assertTrue(picks)
        for clip in picks:
            self.assertTrue(all(clip['end']<=start or clip['start']>=end for start,end in previous))

    def test_fresh_pass_uses_short_gaps_between_rejected_clips(self):
        picks=choose(90,np.ones(90),np.ones(90),[],'',exclude=[(0,30),(55,90)])
        self.assertEqual(len(picks),1)
        self.assertEqual((picks[0]['start'],picks[0]['end']),(30.,55.))

    def test_captions_are_clamped_and_relative(self):
        cues = captions_for([dict(start=9., end=10.5, text='hello'), dict(start=29., end=31., text='world')], 10, 30)
        self.assertEqual(cues, [dict(start=0, end=.5, text='hello'), dict(start=19, end=20, text='world')])

    def test_ass_time_carries_to_next_minute(self):
        self.assertEqual(ass_time(59.999), '0:01:00.00')

    def test_render_sanitizes_ass_control_sequences(self):
        with TemporaryDirectory() as folder:
            target = Path(folder)/'preview.mp4'
            clip = SimpleNamespace(start=0,end=25,framing='fit',captions=[dict(start=0,end=2,text=r'{\pos(0,0)} unsafe')])
            with patch('media.subprocess.run') as run:
                run.return_value.returncode = 0
                render(Path(folder)/'source.mp4', clip, target)
            result = target.with_suffix('.ass').read_text()
            self.assertNotIn(r'\pos', result)
            self.assertIn('1080', result)
            self.assertNotIn(',,VI Clips', result)
            command=run.call_args.args[0]
            self.assertIn(str(WATERMARK),command)
            self.assertIn('overlay=W-w-12:H-h-12',command[command.index('-filter_complex')+1])

    def test_logo_is_visible_at_lower_right_on_first_and_last_frames(self):
        with TemporaryDirectory() as folder:
            source=Path(folder)/'black.mp4'
            output=Path(folder)/'watermarked.mp4'
            run(['ffmpeg','-y','-v','error','-f','lavfi','-i','color=black:size=320x180:rate=10','-t','1','-c:v','libx264','-threads','1',str(source)])
            clip=SimpleNamespace(start=0,end=1,framing='fit',captions=[])
            render(source,clip,output)
            for timestamp in ('0','0.8'):
                raw=run(['ffmpeg','-v','error','-ss',timestamp,'-i',str(output),'-frames:v','1','-pix_fmt','rgb24','-f','rawvideo','-'])
                frame=np.frombuffer(raw,dtype=np.uint8).reshape(640,360,3)
                self.assertGreater(float(frame[545:630,265:350].std()),20)
                self.assertLess(float(frame[545:630,10:90].std()),7)

if __name__ == '__main__':
    unittest.main()
