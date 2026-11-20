"""Probe the private disposable Cobalt container with a public link only."""
import os
import json
from urllib.request import Request,urlopen
from urllib.error import HTTPError

endpoint='http://vi-clips-cobalt-probe:9000/'
request=Request(endpoint,data=json.dumps({'url':os.environ['TEST_YOUTUBE_URL'],
    'videoQuality':'1080','youtubeVideoCodec':'h264','localProcessing':'disabled'}).encode(),
    headers={'Content-Type':'application/json','Accept':'application/json'})
try:
    try:
        with urlopen(request,timeout=30) as response:
            data=json.load(response)
    except HTTPError as exc:
        data=json.load(exc)
    print('COBALT_STATUS',data.get('status'),flush=True)
    if data.get('status')=='error':
        print('COBALT_ERROR',data.get('error',{}).get('code'),flush=True)
    elif data.get('status') in {'tunnel','redirect'}:
        with urlopen(data['url'],timeout=30) as response:
            print('COBALT_STREAM',response.status,len(response.read(65536)),flush=True)
    else:
        print('COBALT_NO_SERVER_STREAM',flush=True)
except Exception as exc:
    print('COBALT_PROBE_FAILED',type(exc).__name__,flush=True)
