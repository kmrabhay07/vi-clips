"""Bound Whisper/VAD memory by decoding a few minutes of audio at a time."""
import logging
import wave
from pathlib import Path

log=logging.getLogger('vi-clips.transcription')
CHUNK_SECONDS=300

def transcribe_chunks(model,wav,beam_size=1,checkpoint=None,on_checkpoint=None):
    checkpoint=checkpoint or {}
    words=list(checkpoint.get('words',[]))
    with wave.open(str(wav),'rb') as source:
        rate=source.getframerate()
        total=source.getnframes()
        completed=int(checkpoint.get('completed_frames',0))
        if not 0<=completed<=total or completed<total and completed%(rate*CHUNK_SECONDS):
            completed=0;words=[]
        chunk=Path(wav).with_suffix('.chunk.wav')
        try:
            while completed<total:
                source.setpos(completed)
                count=min(rate*CHUNK_SECONDS,total-completed)
                with wave.open(str(chunk),'wb') as output:
                    output.setparams(source.getparams())
                    output.writeframes(source.readframes(count))
                offset=completed/rate
                segments,_=model.transcribe(str(chunk),word_timestamps=True,vad_filter=True,beam_size=beam_size)
                for segment in segments:
                    for word in segment.words or []:
                        start=max(offset,offset+word.start)
                        end=min((completed+count)/rate,offset+word.end)
                        if end>start:
                            words.append(dict(start=start,end=end,text=word.word))
                completed+=count
                if on_checkpoint:
                    on_checkpoint(dict(completed_frames=completed,words=words),completed/rate,total/rate)
                log.info('Transcription progress: %.1f / %.1f minutes',completed/rate/60,total/rate/60)
        finally:
            chunk.unlink(missing_ok=True)
    return words
