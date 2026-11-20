"""Reproducible render throughput check; never writes to the user's media volume."""
import argparse
import json
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from media import run, render, probe
from settings import performance

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, help='Optional real gameplay file')
    parser.add_argument('--seconds', type=int, default=20)
    parser.add_argument('--clips', type=int, default=4)
    parser.add_argument('--full', action='store_true', help='Benchmark 1080x1920 instead of previews')
    args = parser.parse_args()
    if not 1 <= args.clips <= 15 or not 1 <= args.seconds <= 45:
        parser.error('Use 1–15 clips and 1–45 seconds')
    with tempfile.TemporaryDirectory(prefix='vi-clips-benchmark-') as directory:
        directory = Path(directory)
        source = args.source.resolve() if args.source else directory / 'synthetic.mp4'
        if not args.source:
            run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i',
                 'testsrc2=size=1280x720:rate=30', '-t', str(args.seconds),
                 '-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '1', str(source)])
        duration, _ = probe(source)
        if duration < args.seconds:
            parser.error('Source is shorter than the benchmark duration')
        def batch(concurrency, name):
            def task(index):
                start = (duration - args.seconds) * index / max(1, args.clips - 1)
                clip = SimpleNamespace(start=start, end=start+args.seconds, framing='fit',
                                       captions=[dict(start=0, end=3, text='A close call')])
                render(source, clip, directory / f'{name}-{index}.mp4', args.full)
            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                list(pool.map(task, range(args.clips)))
            return time.perf_counter() - started
        # Alternate ordering to reduce warm-cache bias; use totals from two trials.
        serial = batch(1, 'serial-a')
        parallel = batch(performance.concurrency, 'parallel-a')
        parallel += batch(performance.concurrency, 'parallel-b')
        serial += batch(1, 'serial-b')
        print(json.dumps(dict(source='real' if args.source else 'synthetic',
                              full_resolution=args.full, clips_per_trial=args.clips,
                              seconds_per_clip=args.seconds, trials=2,
                              concurrency=performance.concurrency,
                              ffmpeg_threads=performance.ffmpeg_threads,
                              serial_seconds=round(serial, 3),
                              parallel_seconds=round(parallel, 3),
                              speedup=round(serial / parallel, 3)), indent=2))

if __name__ == '__main__':
    main()
