import logging
import re
import json
import shutil
import time
from html import unescape
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from media import probe, run
from settings import performance
from contextlib import nullcontext
from youtube_cookies import download_cookie_file
from storage import ensure_space

log = logging.getLogger('vi-clips.youtube')
MAX_BYTES = 20 * 1024 ** 3
DOWNLOAD_FORMAT = 'bv[height=1080][vcodec^=avc1]+ba[acodec^=mp4a]/b[height=1080][vcodec^=avc1]/bv[height=1080]+ba/b[height=1080]'

def video_id(url):
    text = unescape(url).replace('\u200b','').replace('\ufeff','').strip()
    markdown = re.fullmatch(r'\[[^\]]*\]\(\s*(https?://[^\s)]+)\s*\)', text)
    if markdown:
        text = markdown.group(1)
    text = text.strip('`<> \r\n')
    if re.match(r'^(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/',text,re.I):
        text = 'https://' + text
    parsed = urlparse(text)
    host = (parsed.hostname or '').lower()
    if parsed.scheme not in {'https', 'http'} or parsed.username or parsed.password or parsed.port not in (None, 80, 443):
        raise ValueError('Enter a valid YouTube URL')
    parts = parsed.path.strip('/').split('/')
    value = ''
    if host in {'youtu.be', 'www.youtu.be'} and len(parts) == 1:
        value = parts[0]
    elif host in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'}:
        if parsed.path.rstrip('/') == '/watch':
            value = parse_qs(parsed.query).get('v', [''])[0]
        elif len(parts) == 2 and parts[0] in {'shorts', 'embed', 'live'}:
            value = parts[1]
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', value):
        raise ValueError('Enter a YouTube watch, Shorts, live-video, or youtu.be URL')
    return value

def reusable_mp4(path):
    if path.suffix.lower() != '.mp4':
        return False
    info=json.loads(run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)]))
    streams=info.get('streams',[])
    video=next((stream for stream in streams if stream['codec_type']=='video'),{})
    audio=[stream for stream in streams if stream['codec_type']=='audio']
    return ('mp4' in info.get('format',{}).get('format_name','').split(',') and
            video.get('codec_name') in {'h264','av1','vp9'} and
            all(stream.get('codec_name')=='aac' for stream in audio))

def prepare_video(source, destination):
    """Repackage compatible video without encoding every frame of the original."""
    replacing=destination.stat().st_size if destination.is_file() else 0
    ensure_space(destination.parent,max(0,source.stat().st_size-replacing))
    info=json.loads(run(['ffprobe','-v','error','-show_streams','-of','json',str(source)]))
    video=next(stream for stream in info['streams'] if stream['codec_type']=='video')
    audio=next((stream for stream in info['streams'] if stream['codec_type']=='audio'),None)
    video_args=['-c:v','copy'] if video['codec_name'] in {'h264','av1','vp9'} else ['-c:v','libx264','-preset','veryfast','-crf','20','-pix_fmt','yuv420p','-threads',str(performance.ffmpeg_threads)]
    audio_args=['-c:a','copy'] if audio and audio['codec_name']=='aac' else ['-c:a','aac']
    run(['ffmpeg','-y','-v','error','-threads',str(performance.ffmpeg_threads),'-i',str(source),
         '-map','0:v:0','-map','0:a:0?',*video_args,*audio_args,'-movflags','+faststart',str(destination)])
    duration,_=probe(destination)
    if duration < 20 or destination.stat().st_size > MAX_BYTES:
        raise ValueError('Choose a video between 20 seconds and 20 GB')
    return duration

def download_video(id, destination, on_update=None, use_cookies=False):
    from yt_dlp import YoutubeDL
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', id):
        raise ValueError('Invalid YouTube video ID')
    last_space_check=[0.]
    def progress(event):
        now=time.monotonic()
        if now-last_space_check[0]>=2:
            ensure_space(destination.parent)
            last_space_check[0]=now
        if event.get('downloaded_bytes', 0) > MAX_BYTES:
            raise ValueError('YouTube import exceeds the 20 GB limit')
        if on_update:
            on_update({'stage':'downloading','completed':event.get('downloaded_bytes',0),
                       'total':event.get('total_bytes') or event.get('total_bytes_estimate') or 0})
    def allowed(info, *, incomplete=False):
        if info.get('is_live') or info.get('live_status') in {'is_live', 'is_upcoming'}:
            return 'Wait until the live stream has ended before importing it'
        if info.get('duration') and info['duration'] < 20:
            return 'Choose a video at least 20 seconds long'
        formats=info.get('requested_formats') or [info]
        estimated=sum(f.get('filesize') or f.get('filesize_approx') or 0 for f in formats)
        if estimated:
            # Resume existing components instead of charging their space twice.
            cached=sum(p.stat().st_size for p in folder.iterdir() if p.is_file() and
                       re.fullmatch(r'source(?:\.f[\w-]+)?\.(mp4|m4a|webm)(\.part)?',p.name))
            replacing=folder/'source.temp.mp4'
            credit=replacing.stat().st_size if len(formats)>1 and replacing.is_file() else 0
            ensure_space(destination.parent,max(0,estimated*(2 if len(formats)>1 else 1)-cached-credit))
    folder=destination.parent/'youtube-cache'/id
    folder.mkdir(parents=True,exist_ok=True)
    metadata=folder/'metadata.json'
    def postprocessing(event):
        if event.get('status')=='started':
            components=[p for p in folder.iterdir() if re.fullmatch(r'source\.f[\w-]+\.(mp4|m4a|webm)',p.name)]
            replacing=folder/'source.temp.mp4'
            credit=replacing.stat().st_size if replacing.is_file() else 0
            ensure_space(folder,max(0,sum(p.stat().st_size for p in components)-credit))
    try:
        files=[p for p in folder.iterdir() if p.name in {'source.mp4','source.mkv','source.webm'}]
        info=json.loads(metadata.read_text()) if metadata.is_file() else {}
        if not files:
            options = dict(outtmpl=str(folder/'source.%(ext)s'),
                format=DOWNLOAD_FORMAT,
                merge_output_format='mp4', noplaylist=True, quiet=True,
                socket_timeout=30, retries=3, fragment_retries=3,
                concurrent_fragment_downloads=4, max_filesize=MAX_BYTES,
                source_address='0.0.0.0',
                progress_hooks=[progress], postprocessor_hooks=[postprocessing], match_filter=allowed,
                js_runtimes={'node': {}}, logger=log)
            # Only sources explicitly connected to the browser bridge use sessions.
            with (download_cookie_file() if use_cookies else nullcontext(None)) as cookiefile:
                if cookiefile:
                    options['cookiefile'] = cookiefile
                elif use_cookies:
                    raise ValueError('Reconnect your YouTube browser session before retrying')
                with YoutubeDL(options) as downloader:
                    info = downloader.extract_info(f'https://www.youtube.com/watch?v={id}', download=True)
            if not info:
                raise ValueError('YouTube returned no downloadable video')
            metadata.write_text(json.dumps({'title':info.get('title'),'duration':info.get('duration')}))
            files=[p for p in folder.iterdir() if p.name in {'source.mp4','source.mkv','source.webm'}]
        if len(files)!=1:
            raise ValueError('YouTube import did not produce a complete video file')
        duration,_=probe(files[0])
        if duration < 20 or files[0].stat().st_size > MAX_BYTES:
            raise ValueError('Choose a video between 20 seconds and 20 GB')
        if on_update:
            on_update({'stage':'preparing','title':info.get('title') or f'YouTube {id}',
                       'duration':duration,'completed':files[0].stat().st_size,'total':files[0].stat().st_size})
        if reusable_mp4(files[0]):
            # Same-volume rename: no second full recording copy or faststart pass.
            files[0].replace(destination)
        else:
            output=folder/'prepared.mp4'
            duration=prepare_video(files[0],output)
            output.replace(destination)
        title=str(info.get('title') or f'YouTube {id}')[:250]
        # Delete only this importer-owned cache after the final original is verified.
        if folder.resolve().parent != (destination.parent/'youtube-cache').resolve():
            raise ValueError('Import cache escaped storage')
        shutil.rmtree(folder)
        return title,duration
    except Exception as exc:
        if any(message in str(exc).lower() for message in ('not a bot', 'sign in to confirm', 'login required', 'cookies for authentication')):
            if use_cookies:
                raise RuntimeError('YouTube blocked this server even with your browser session. Sign in to YouTube, then retry to refresh the session, or upload the original MP4. The download cache is kept for retry.') from exc
            raise RuntimeError('YouTube blocked this anonymous download. Connect your YouTube browser session and retry, or upload the original MP4. The download cache is kept for retry.') from exc
        if 'Requested format is not available' in str(exc):
            raise RuntimeError('YouTube did not offer a downloadable 1080p version. No lower-resolution video was downloaded. Upload the original 1080p MP4 instead.') from exc
        raise RuntimeError(f'YouTube import failed: {str(exc)[-1000:]}. The download cache is kept for retry. If YouTube requires sign-in or blocks this server, upload the original MP4 instead.') from exc
