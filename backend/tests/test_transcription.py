import unittest
import wave
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from transcription import transcribe_chunks
from media import audio_signal

class TranscriptionTests(unittest.TestCase):
    def test_valid_extracted_audio_is_reused_after_worker_crash(self):
        with TemporaryDirectory() as folder:
            source=Path(folder)/'source.mp4';source.write_bytes(b'fixture')
            wav=source.with_suffix('.wav')
            with wave.open(str(wav),'wb') as output:
                output.setparams((1,2,16000,0,'NONE','not compressed'))
                output.writeframes(b'\0\0'*16000*21)
            with patch('media.run',side_effect=AssertionError('Must reuse extracted WAV')):
                signal,path=audio_signal(source,21,True)
            self.assertEqual(len(signal),21);self.assertEqual(path,wav)
            os.utime(source,ns=(wav.stat().st_mtime_ns+1000,wav.stat().st_mtime_ns+1000))
            with patch('media.run',side_effect=RuntimeError('Extraction required')):
                with self.assertRaisesRegex(RuntimeError,'Extraction required'):audio_signal(source,21,True)

    def test_bounds_chunks_offsets_words_and_resumes_after_failure(self):
        with TemporaryDirectory() as folder:
            wav=Path(folder)/'source.wav'
            with wave.open(str(wav),'wb') as out:
                out.setparams((1,2,100,0,'NONE','not compressed'))
                out.writeframes(b'\0\0'*750)
            checkpoints=[];calls=[]
            class Model:
                def transcribe(self,path,**kwargs):
                    with wave.open(path,'rb') as audio:
                        seconds=audio.getnframes()/audio.getframerate()
                    calls.append(seconds)
                    if len(calls)==2:
                        raise RuntimeError('interrupted')
                    word=SimpleNamespace(start=.1,end=.5,word='hello')
                    return iter([SimpleNamespace(words=[word])]),None
            def save(checkpoint,*args):
                checkpoints.append({**checkpoint,'words':list(checkpoint['words'])})
            with patch('transcription.CHUNK_SECONDS',3):
                with self.assertRaises(RuntimeError):transcribe_chunks(Model(),wav,on_checkpoint=save)
                self.assertEqual(checkpoints[-1]['completed_frames'],300)
                calls.clear()
                class Resumed:
                    def transcribe(self,path,**kwargs):
                        with wave.open(path,'rb') as audio:
                            calls.append(audio.getnframes()/audio.getframerate())
                        return iter([SimpleNamespace(words=[SimpleNamespace(start=.1,end=.5,word='hello')])]),None
                words=transcribe_chunks(Resumed(),wav,checkpoint=checkpoints[-1],on_checkpoint=save)
            self.assertEqual(calls,[3,1.5])
            self.assertEqual([round(w['start'],1) for w in words],[.1,3.1,6.1])
            self.assertEqual(checkpoints[-1]['completed_frames'],750)
            self.assertFalse(wav.with_suffix('.chunk.wav').exists())
