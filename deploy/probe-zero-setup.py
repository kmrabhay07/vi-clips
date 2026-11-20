"""Disposable server-only probe; no application data or cookie mounts."""
import os
import subprocess
import sys
subprocess.run([sys.executable,'-m','pip','install','--quiet','--pre','--upgrade',
                '--target','/tmp/zero-setup','yt-dlp[default]'],check=True)
sys.path.insert(0,'/tmp/zero-setup')
from yt_dlp import YoutubeDL
from yt_dlp.version import __version__
from yt_dlp.networking import Request

class Log:
    def debug(self,*args): pass
    def warning(self,*args): pass
    def error(self,*args): pass

print('PROBE_VERSION',__version__,flush=True)
for client in ['default','visionos','tv_samsung']:
    options=dict(quiet=True,noplaylist=True,js_runtimes={'node':{}},
                 format='bv[height=1080]+ba/b[height=1080]',
                 socket_timeout=8,retries=0,extractor_retries=0,logger=Log())
    if client!='default':
        options['extractor_args']={'youtube':{'player_client':[client]}}
    try:
        with YoutubeDL(options) as y:
            info=y.extract_info(os.environ['TEST_YOUTUBE_URL'],download=False)
            for stream in info.get('requested_formats') or [info]:
                headers={**info.get('http_headers',{}),**stream.get('http_headers',{}),
                         'Range':'bytes=0-65535'}
                with y.urlopen(Request(stream['url'],headers=headers)) as response:
                    print('STREAM_OK',client,stream.get('height') or 'audio',response.status,
                          len(response.read(65536)),flush=True)
    except Exception as exc:
        message=str(exc)
        reason='BOT_BLOCKED' if 'not a bot' in message else message.split('https://')[0][:180]
        print('CLIENT_FAILED',client,reason,flush=True)
