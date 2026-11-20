"""Word animation aligned to the saved transcript, with honest edited-text fallback."""
import re


def word_events(cues, words, clip_start):
    events=[]
    if cues:
        lower=clip_start+min(cue['start'] for cue in cues)
        upper=clip_start+max(cue['end'] for cue in cues)
        words=[word for word in words if word['end']>lower and word['start']<upper]
    def token(value):
        return ''.join(re.findall(r'\w+',value.casefold()))
    for cue in cues:
        texts=cue['text'].split()
        selected=[w for w in words if w['end']>clip_start+cue['start'] and w['start']<clip_start+cue['end']]
        if not texts:
            continue
        if len(texts)!=len(selected) or [token(t) for t in texts]!=[token(w['text']) for w in selected]:
            events.append(dict(start=cue['start'],end=cue['end'],tokens=texts,active_index=None))
            continue
        previous=cue['start']
        for index,word in enumerate(selected):
            group_start=index//3*3
            group_end=min(len(texts),group_start+3)
            start=max(previous,cue['start'],word['start']-clip_start)
            end=min(cue['end'],selected[index+1]['start']-clip_start if index+1<len(selected) else word['end']-clip_start+.12)
            if end<=start:
                continue
            events.append(dict(start=round(start,6),end=round(end,6),tokens=texts[group_start:group_end],active_index=index-group_start))
            previous=end
    return events
