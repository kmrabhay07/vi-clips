"""Disposable cookie-free OCI probe; does not access app DB or media volumes."""
import os
import subprocess
import sys

subprocess.run([sys.executable, '-m', 'pip', 'install', '--quiet', '--target',
                '/tmp/pot-plugin', 'bgutil-ytdlp-pot-provider==2.0.2'], check=True)
sys.path.insert(0, '/tmp/pot-plugin')
from yt_dlp import YoutubeDL
from yt_dlp.networking import Request

class Log:
    def debug(self, message):
        if 'Generating POT via HTTP server' in message:
            print('PROVIDER_REQUESTED', flush=True)
        elif 'Generated POT:' in message:
            print('PROVIDER_GENERATED_TOKEN', flush=True)
    def warning(self, message):
        print('WARNING', message.split('https://')[0][:180], flush=True)
    def error(self, message):
        pass

try:
    with YoutubeDL(dict(quiet=True, noplaylist=True, verbose=True,
                        format='bv[height=1080]+ba/b[height=1080]',
                        js_runtimes={'node': {}}, socket_timeout=12,
                        retries=0, extractor_retries=0, logger=Log(),
                        extractor_args={'youtube': {'player_client': ['mweb'], 'fetch_pot': ['always'], 'pot_trace': ['true']},
                                        'youtubepot-bgutilhttp': {'base_url': ['http://vi-clips-pot-probe:4416']}})) as y:
        info = y.extract_info(os.environ['TEST_YOUTUBE_URL'], download=False)
        for f in info.get('requested_formats') or [info]:
            headers = {**info.get('http_headers', {}), **f.get('http_headers', {}),
                       'Range': 'bytes=0-65535'}
            with y.urlopen(Request(f['url'], headers=headers)) as response:
                print('STREAM_OK', f.get('height') or 'audio', response.status,
                      len(response.read(65536)), flush=True)
except Exception as exc:
    error = str(exc)
    print('PROBE_FAILED', 'BOT_BLOCKED' if 'not a bot' in error else error.split('https://')[0][:220], flush=True)
