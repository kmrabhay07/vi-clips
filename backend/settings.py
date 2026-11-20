import os
from dataclasses import dataclass

def integer(name, default, maximum):
    value = int(os.getenv(name, str(default)))
    if not 1 <= value <= maximum:
        raise ValueError(f'{name} must be between 1 and {maximum}')
    return value

@dataclass(frozen=True)
class Performance:
    concurrency: int = 2
    ffmpeg_threads: int = 1
    whisper_threads: int = 1
    whisper_beam_size: int = 1

    @classmethod
    def from_env(cls):
        return cls(concurrency=integer('WORKER_CONCURRENCY', 2, 8),
                   ffmpeg_threads=integer('FFMPEG_THREADS', 1, 16),
                   whisper_threads=integer('WHISPER_THREADS', 1, 16),
                   whisper_beam_size=integer('WHISPER_BEAM_SIZE', 1, 5))

    def public(self):
        return dict(concurrency=self.concurrency, analysis_limit=1,
                    ffmpeg_threads=self.ffmpeg_threads,
                    whisper_threads=self.whisper_threads,
                    whisper_beam_size=self.whisper_beam_size)

performance = Performance.from_env()
