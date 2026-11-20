"""User-supplied YouTube cookies, encrypted at rest and temporary during download."""
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from sqlalchemy import delete
from db import Session, IntegrationSecret
from youtube_auth import read_secret, write_secret

KEY = 'youtube_import_cookies'


def cookie_status():
    document = read_secret(KEY)
    return {'configured': bool(document), 'saved_at': document.get('saved_at') if document else None}


def save_cookies(raw):
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeError:
        raise ValueError('Choose a Netscape cookies.txt export') from None
    if '\x00' in text or not text.startswith(('# Netscape HTTP Cookie File', '# HTTP Cookie File')):
        raise ValueError('Choose a Netscape cookies.txt export, not JSON or a browser database')
    rows = []
    for line in text.splitlines():
        if not line.strip() or line.startswith('#') and not line.startswith('#HttpOnly_'):
            continue
        parts = line.removeprefix('#HttpOnly_').split('\t')
        if len(parts) != 7:
            raise ValueError('Invalid Netscape cookie row; export the file again')
        domain, subdomains, path, secure, expires, name, value = parts
        if subdomains not in {'TRUE', 'FALSE'} or secure not in {'TRUE', 'FALSE'} or not path.startswith('/') or not expires.isdigit() or not name:
            raise ValueError('Invalid Netscape cookie row; export the file again')
        host = domain.lstrip('.').lower()
        if not any(host == root or host.endswith('.'+root) for root in ('youtube.com', 'google.com')):
            continue
        if int(expires) and int(expires) <= time.time():
            continue
        rows.append('\t'.join(parts))
    if not rows:
        raise ValueError('No current YouTube or Google cookies found. Export cookies from your signed-in YouTube session.')
    with Session.begin() as db:
        write_secret(db, KEY, {'text': '# Netscape HTTP Cookie File\n'+'\n'.join(rows)+'\n', 'saved_at': int(time.time())})
    return cookie_status()


def remove_cookies():
    with Session.begin() as db:
        db.execute(delete(IntegrationSecret).where(IntegrationSecret.name == KEY))
    return cookie_status()


@contextmanager
def download_cookie_file():
    document = read_secret(KEY)
    if not document:
        yield None
        return
    fd, name = tempfile.mkstemp(prefix='vi-youtube-', suffix='.txt')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            file.write(document['text'])
        yield name
    finally:
        Path(name).unlink(missing_ok=True)
