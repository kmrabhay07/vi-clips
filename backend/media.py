import json
import re
import subprocess
import wave
import math
from bisect import bisect_left,bisect_right
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from settings import performance
from caption_events import word_events

WATERMARK = Path(__file__).resolve().parent / 'assets' / 'watermark.png'

def run(args):
    result = subprocess.run(args, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors='replace')[-2500:])
    return result.stdout

def probe(path):
    info = json.loads(run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)]))
    if not any(s['codec_type'] == 'video' for s in info['streams']):
        raise ValueError('The file contains no video stream')
    return float(info['format']['duration']), any(s['codec_type'] == 'audio' for s in info['streams'])

def motion_signal(path):
    import numpy as np
    # One grayscale frame per second; bounded memory even for long sessions.
    motion = []
    proc = subprocess.Popen(['ffmpeg', '-v', 'error', '-threads', str(performance.ffmpeg_threads), '-i', str(path), '-filter_threads', '1', '-vf', 'fps=1,scale=160:90,format=gray', '-threads', str(performance.ffmpeg_threads), '-f', 'rawvideo', '-'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    previous = None
    try:
        while True:
            raw = proc.stdout.read(14400)
            if len(raw) != 14400:
                break
            frame = np.frombuffer(raw, dtype=np.uint8).astype(float)
            motion.append(0 if previous is None else float(np.mean(np.abs(frame - previous))))
            previous = frame
    finally:
        proc.stdout.close()
        proc.wait()
    if proc.returncode:
        raise RuntimeError('Motion analysis failed')
    return np.array(motion)

def audio_signal(path, duration, has_audio):
    import numpy as np
    audio = np.zeros(math.ceil(duration))
    wav = path.with_suffix('.wav')
    if has_audio:
        reusable=False
        if wav.is_file() and wav.stat().st_mtime_ns>=path.stat().st_mtime_ns:
            try:
                with wave.open(str(wav)) as cached:
                    reusable=(cached.getnchannels()==1 and cached.getsampwidth()==2 and cached.getframerate()==16000
                              and abs(cached.getnframes()/16000-duration)<2
                              and wav.stat().st_size>=44+cached.getnframes()*2)
            except (OSError,EOFError,wave.Error):
                pass
        if not reusable:
            run(['ffmpeg', '-y', '-v', 'error', '-threads', str(performance.ffmpeg_threads), '-i', str(path), '-vn', '-ar', '16000', '-ac', '1', str(wav)])
        with wave.open(str(wav)) as stream:
            for i in range(len(audio)):
                data = np.frombuffer(stream.readframes(16000), dtype=np.int16).astype(float)
                audio[i] = np.sqrt(np.mean(data ** 2)) if len(data) else 0
    return audio, wav

def align_audio(audio, length):
    import numpy as np
    return np.pad(audio[:length], (0, max(0, length - len(audio))))

def signals(path, duration, has_audio):
    with ThreadPoolExecutor(max_workers=2) as pool:
        visual = pool.submit(motion_signal, path)
        spoken = pool.submit(audio_signal, path, duration, has_audio)
        motion = visual.result()
        audio, wav = spoken.result()
    return motion, align_audio(audio, len(motion)), wav

def choose(duration, motion, audio, words, prompt, limit=15, exclude=()):
    import numpy as np
    if duration < 20 or not len(motion):
        return []
    def normalize(x):
        top = max(float(np.percentile(x, 95)),float(np.max(x))*.25)
        return np.clip(x / top, 0, 1) if top > 0 else np.zeros(len(x))
    activity = .6 * normalize(motion) + .4 * normalize(audio)
    tokens = set(re.findall(r'\w+', prompt.lower())) - {'and', 'the', 'with', 'clips'}
    ordered=sorted(words,key=lambda word:word['start'])
    word_starts=[word['start'] for word in ordered]
    word_ends=sorted(word['end'] for word in words if re.search(r'[.!?]$',word['text']))
    candidates = []
    starts=set(range(0,max(1,int(duration)-19),5))
    baseline=float(np.median(activity))
    for index in range(1,len(activity)-1):
        if activity[index]>baseline+.15 and activity[index]>=activity[index-1] and activity[index]>activity[index+1]:
            proposed=max(0,min(duration-20,index-6))
            near=word_starts[bisect_left(word_starts,proposed-1):bisect_right(word_starts,proposed+1)]
            starts.add(float(min(near,key=lambda t:abs(t-proposed)) if near else proposed))
    for start in sorted(starts):
        if any(old_start <= start < old_end for old_start,old_end in exclude):
            continue
        boundary = min([duration, *[old_start for old_start,_ in exclude if old_start > start]])
        if boundary - start < 20:
            continue
        end = min(boundary, start + 30)
        near = word_ends[bisect_left(word_ends,start+20):bisect_right(word_ends,min(boundary,start+40))]
        if near:
            end = min(near, key=lambda t: abs(t - end))
        if any(start < old_end and end > old_start for old_start, old_end in exclude):
            continue
        signal=activity[int(start):min(len(activity),math.ceil(end))]
        score = float(np.mean(signal))
        peak=int(start)+int(np.argmax(signal))
        if activity[peak]>baseline+.15:
            score+=.16*float(activity[peak]-baseline)+.06*max(0,1-abs(peak-start-6)/6)
        spoken = ' '.join(w['text'] for w in ordered[bisect_left(word_starts,start):bisect_left(word_starts,end)]).lower()
        matched = tokens.intersection(re.findall(r'\w+', spoken))
        reactions=sum(phrase in spoken for phrase in ('no way','what just happened','that was close','oh my god','holy crap'))
        score = min(1., score + min(.2, .05 * len(matched)) + min(.12,.04*reactions))
        if score < .22:
            continue
        reason='Motion and audio activity'
        if activity[peak]>baseline+.15:
            reason+='; includes setup before an activity peak'
        if reactions:
            reason+='; commentary reaction'
        if matched:
            reason+='; matches: '+', '.join(sorted(matched))
        candidates.append(dict(start=float(start), end=float(end), score=round(score * 100), reason=reason))
    selected = []
    for candidate in sorted(candidates, key=lambda x: x['score'], reverse=True):
        if all(candidate['end'] <= x['start'] or candidate['start'] >= x['end'] for x in selected):
            selected.append(candidate)
        if len(selected) == limit:
            break
    return selected

def captions_for(words, start, end):
    inside = [w for w in words if w['end'] > start and w['start'] < end]
    groups, group = [], []
    for word in inside:
        if group and (len(group) >= 6 or word['start'] - group[-1]['end'] > .8 or word['end'] - group[0]['start'] > 3):
            groups.append(group)
            group = []
        group.append(word)
    if group:
        groups.append(group)
    return [dict(start=max(0, group[0]['start'] - start), end=min(end-start, group[-1]['end']-start), text=' '.join(w['text'].strip() for w in group)) for group in groups]

def ass_time(t):
    cs = round(t * 100)
    return f'{cs//360000}:{cs//6000%60:02}:{cs//100%60:02}.{cs%100:02}'

def retime_captions(cues, old_start, old_end, start, end, words):
    """Keep edited text at its source time; transcribe newly included sections."""
    absolute = [dict(start=cue['start']+old_start, end=cue['end']+old_start, text=cue['text']) for cue in cues]
    if start < old_start:
        absolute.extend(dict(start=c['start']+start,end=c['end']+start,text=c['text']) for c in captions_for(words,start,min(end,old_start)))
    if end > old_end:
        boundary=max(start,old_end)
        absolute.extend(dict(start=c['start']+boundary,end=c['end']+boundary,text=c['text']) for c in captions_for(words,boundary,end))
    result=[]
    previous=0
    duration=round(end-start,6)
    for cue in sorted(absolute,key=lambda c:c['start']):
        left=round(max(0,previous,cue['start']-start),6)
        right=round(min(duration,cue['end']-start),6)
        if right > left:
            result.append(dict(start=left,end=right,text=cue['text']))
            previous=right
    return result

def render(source, clip, output, full=False, words=None):
    landscape = clip.framing == 'landscape'
    canvas_width, canvas_height = (1920, 1080) if landscape else (1080, 1920)
    width, height = (canvas_width, canvas_height) if full else ((640, 360) if landscape else (360, 640))
    style = getattr(clip, 'caption_style', 'highlight')
    primary = '&H0000FFFF' if style == 'yellow' else '&H00FFFFFF'
    border = 3 if style == 'box' else 1
    caption_size = round((52 if landscape else 60) * getattr(clip,'caption_scale',1))
    caption_margin = 70 if landscape else 440
    position = getattr(clip,'caption_position','bottom')
    if position == 'top':
        caption_margin = canvas_height - 140
    elif position == 'middle':
        caption_margin = canvas_height // 2
    elif position == 'lower_middle':
        caption_margin = round(canvas_height * .25)
    logo_width = max(24, round(150 * width / canvas_width))
    logo_margin = max(8, round(36 * width / canvas_width))
    if not WATERMARK.is_file():
        raise FileNotFoundError('The VI Clips watermark image is missing')
    # ASS coordinates stay in the same design space for preview and final.
    header = f'''[Script Info]
ScriptType: v4.00+
PlayResX: {canvas_width}
PlayResY: {canvas_height}
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,{caption_size},{primary},&H00FFFFFF,&H00140F20,&H80000000,-1,0,0,0,100,100,0,0,{border},4,1,2,60,60,{caption_margin},1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    lines = []
    for cue in ([] if style in {'none','active','pop'} else clip.captions):
        safe = re.sub(r'[{}\\\r\n]', ' ', cue['text']).strip().split()
        if safe and style == 'highlight':
            safe[-1] = '{\\c&HBA73FF&}' + safe[-1]
        lines.append(f"Dialogue: 0,{ass_time(cue['start'])},{ass_time(cue['end'])},Default,,0,0,0,,{' '.join(safe)}")
    if style in {'active','pop'}:
        for event in word_events(clip.captions,words or [],clip.start):
            parts=[]
            for index,text in enumerate(event['tokens']):
                safe=re.sub(r'[{}\\\r\n]',' ',text)
                active=index==event['active_index']
                effect=r'{\c&H00FFFF&}' if active else r'{\c&HFFFFFF&}'
                if style=='pop' and active:
                    effect+=r'{\fscx112\fscy112\t(0,100,\fscx100\fscy100)}'
                else:
                    effect+=r'{\fscx100\fscy100}'
                parts.append(effect+safe)
            lines.append(f"Dialogue: 0,{ass_time(event['start'])},{ass_time(event['end'])},Default,,0,0,0,,{' '.join(parts)}")
    ass = output.with_suffix('.ass')
    ass.write_text(header + '\n'.join(lines), encoding='utf-8')
    if clip.framing == 'crop':
        x = getattr(clip, 'crop_x', 0.5)
        y = getattr(clip, 'crop_y', 0.5)
        segments = getattr(clip,'crop_segments',[]) or []
        def position_expression(axis, fallback):
            if not segments:
                return str(fallback)
            expression=str(segments[-1][axis])
            for index in range(len(segments)-2,-1,-1):
                expression=f"if(lt(t,{segments[index+1]['time']}),{segments[index][axis]},{expression})"
            return expression
        framing = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}:'(iw-ow)*({position_expression('x',x)})':'(ih-oh)*({position_expression('y',y)})'"
    else:
        framing = f'scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x100e18'
    if clip.framing == 'dual':
        from layout import DualCrop
        layout = DualCrop.model_validate(clip.dual_crop)
        top_height = round(height * layout.top_fraction / 2) * 2
        first, second = (layout.face, layout.gameplay) if layout.face_on_top else (layout.gameplay, layout.face)
        def panel(rect, panel_height):
            return (f'crop=w=iw*{rect.width}:h=ih*{rect.height}:x=iw*{rect.x}:y=ih*{rect.y},'
                    f'scale={width}:{panel_height}:force_original_aspect_ratio=increase,'
                    f'crop={width}:{panel_height},setsar=1')
        video_filter=(f'[0:v]split[top_source][bottom_source];'
                      f'[top_source]{panel(first,top_height)}[top_panel];'
                      f'[bottom_source]{panel(second,height-top_height)}[bottom_panel];'
                      f'[top_panel][bottom_panel]vstack=inputs=2,ass={ass.name}[captioned];')
    elif clip.framing == 'blur':
        video_filter=(f'[0:v]split[background][foreground];[background]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},boxblur=20:2[blurred];'
                      f'[foreground]scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2[fit];[blurred][fit]overlay=(W-w)/2:(H-h)/2,ass={ass.name}[captioned];')
    else:
        video_filter=f'[0:v]{framing},ass={ass.name}[captioned];'
    filters = (video_filter+
               f'[1:v]scale={logo_width}:-1:flags=lanczos,format=rgba[logo];'
               f'[captioned][logo]overlay=W-w-{logo_margin}:H-h-{logo_margin}:eof_action=repeat:format=auto,format=yuv420p[v]')
    # Use a safe generated basename and run in its directory to avoid filter escaping paths.
    audio_args = ['-an'] if getattr(clip,'audio_muted',False) else ['-af',f"volume={getattr(clip,'volume',1)}",'-c:a','aac']
    result = subprocess.run(['ffmpeg', '-y', '-v', 'error', '-threads', str(performance.ffmpeg_threads), '-ss', str(clip.start), '-i', str(source), '-i', str(WATERMARK), '-t', str(clip.end-clip.start), '-filter_complex_threads', '1', '-filter_complex', filters, '-map', '[v]', '-map', '0:a:0?', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '22' if full else '28', '-threads', str(performance.ffmpeg_threads), *audio_args, '-movflags', '+faststart', str(output)], cwd=output.parent, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors='replace')[-2500:])
