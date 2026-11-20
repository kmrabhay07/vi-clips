import unittest
from youtube import DOWNLOAD_FORMAT
from yt_dlp import YoutubeDL


class DownloadQualityTests(unittest.TestCase):
    def test_selector_requires_1080_and_keeps_audio(self):
        formats=[{'format_id':str(height),'height':height,'width':height*16//9,'ext':'mp4','vcodec':'avc1','acodec':'none','url':'https://example.com/video'} for height in (360,720,1080,2160)]
        audio={'format_id':'audio','ext':'m4a','vcodec':'none','acodec':'mp4a.40.2','url':'https://example.com/audio'}
        with YoutubeDL({'quiet':True}) as downloader:
            selector=downloader.build_format_selector(DOWNLOAD_FORMAT)
            selected=list(selector({'formats':[audio,*formats],'has_merged_format':False,'incomplete_formats':False}))
            self.assertEqual(len(selected),1)
            self.assertEqual(selected[0]['height'],1080)
            self.assertEqual(len(selected[0]['requested_formats']),2)
            lower=[f for f in formats if f['height']<1080]
            self.assertEqual(list(selector({'formats':[audio,*lower],'has_merged_format':False,'incomplete_formats':False})),[])
