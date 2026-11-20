import unittest
from pathlib import Path
from unittest.mock import patch
from tempfile import TemporaryDirectory
from support import scratch
from fastapi.testclient import TestClient
from db import Session, IntegrationSecret, init
from db import Source,Job
from sqlalchemy import select,delete
from main import app
from youtube_cookies import KEY, save_cookies, remove_cookies, download_cookie_file

COOKIES=b'# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tprivate-test-value\n.example.com\tTRUE\t/\tTRUE\t0\tSID\tunrelated-secret\n'


class ImportCookieTests(unittest.TestCase):
    def setUp(self):
        init()
        remove_cookies()

    def tearDown(self):
        remove_cookies()

    def test_encryption_filtering_and_temporary_file_cleanup(self):
        save_cookies(COOKIES)
        with Session() as db:
            encrypted=db.get(IntegrationSecret,KEY).cipher
            self.assertNotIn('private-test-value',encrypted)
        path=None
        with self.assertRaises(RuntimeError):
            with download_cookie_file() as name:
                path=Path(name)
                self.assertIn('private-test-value',path.read_text())
                self.assertNotIn('unrelated-secret',path.read_text())
                self.assertEqual(path.stat().st_mode & 0o777,0o600)
                raise RuntimeError('simulated download failure')
        self.assertFalse(path.exists())
        remove_cookies()
        with download_cookie_file() as name:
            self.assertIsNone(name)

    def test_api_validation_origin_and_secret_free_status(self):
        with TestClient(app) as client:
            url='/api/youtube/import-cookies'
            self.assertEqual(client.post(url,files={'file':('cookies.txt',COOKIES)},headers={'origin':'https://untrusted.example'}).status_code,403)
            self.assertEqual(client.post(url,files={'file':('cookies.txt',b'not cookies')}).status_code,422)
            self.assertEqual(client.post(url,files={'file':('cookies.txt',b'x'*1048577)}).status_code,413)
            response=client.post(url,files={'file':('cookies.txt',COOKIES)})
            self.assertEqual(response.status_code,200,response.text)
            self.assertTrue(response.json()['configured'])
            self.assertNotIn('private-test-value',client.get('/api/state').text)
            self.assertNotIn('private-test-value',client.get(url).text)
            self.assertFalse(client.delete(url).json()['configured'])

    def test_downloader_never_receives_saved_cookies(self):
        from youtube import download_video
        save_cookies(COOKIES)
        names=[]
        def downloader(options):
            names.append(options)
            self.assertNotIn('cookiefile',options)
            self.assertNotIn('cookiesfrombrowser',options)
            raise RuntimeError('simulated network failure')
        with TemporaryDirectory() as folder:
            with patch('yt_dlp.YoutubeDL',side_effect=downloader):
                with self.assertRaises(RuntimeError):
                    download_video('cookieTest1',Path(folder)/'test-cookie-download.mp4')
        self.assertEqual(len(names),1)

    def test_connected_downloader_uses_temporary_cookie_file_and_removes_it(self):
        from youtube import download_video
        save_cookies(COOKIES)
        names=[]
        def downloader(options):
            names.append(options['cookiefile'])
            self.assertIn('private-test-value',Path(names[-1]).read_text())
            raise RuntimeError('simulated network failure')
        with TemporaryDirectory() as folder,patch('yt_dlp.YoutubeDL',side_effect=downloader):
            with self.assertRaises(RuntimeError):
                download_video('cookieTest1',Path(folder)/'connected.mp4',use_cookies=True)
        self.assertEqual(len(names),1)
        self.assertFalse(Path(names[0]).exists())

    def test_link_requires_saved_session_only_when_explicitly_connected(self):
        with TestClient(app) as client:
            body={'url':'https://youtu.be/cookieTest2','use_browser_cookies':True}
            self.assertEqual(client.post('/api/resolve',json=body).status_code,422)
            save_cookies(COOKIES)
            response=client.post('/api/resolve',json=body)
            self.assertEqual(response.status_code,200,response.text)
            id=response.json()['id']
            try:
                with Session() as db:
                    self.assertTrue(db.get(Source,id).use_browser_cookies)
            finally:
                with Session.begin() as db:
                    db.execute(delete(Job).where(Job.target==id))
                    db.execute(delete(Source).where(Source.id==id))

