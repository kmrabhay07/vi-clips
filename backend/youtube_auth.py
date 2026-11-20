import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import datetime,timedelta,timezone
from urllib.parse import urlencode,urlparse
import httpx
from cryptography.fernet import Fernet
from sqlalchemy import select,delete
from db import DATA,Session,IntegrationSecret,OAuthAttempt

logging.getLogger('httpx').setLevel(logging.WARNING)
SCOPES='https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly'
TOKEN_URL='https://oauth2.googleapis.com/token'

def base_url():
    value=os.getenv('APP_BASE_URL','http://localhost:3000').rstrip('/')
    parsed=urlparse(value)
    if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('APP_BASE_URL must be the public origin of this app')
    if parsed.path not in {'','/'}:
        raise ValueError('APP_BASE_URL must not include a path')
    if parsed.scheme!='https' and parsed.hostname not in {'localhost','127.0.0.1'}:
        raise ValueError('Use HTTPS for a hosted YouTube connection')
    return value

def redirect_uri():
    return base_url()+'/youtube/callback'

def cipher():
    directory=DATA/'.secrets';directory.mkdir(mode=0o700,exist_ok=True)
    path=directory/'encryption.key'
    if not path.exists():
        try:
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as file:
                file.write(Fernet.generate_key())
        except FileExistsError:
            pass
    return Fernet(path.read_bytes())

def seal(value):
    return cipher().encrypt(json.dumps(value).encode()).decode()

def unseal(value):
    return json.loads(cipher().decrypt(value.encode()))

def read_secret(name,db=None):
    if db is not None:
        item=db.get(IntegrationSecret,name)
        return unseal(item.cipher) if item else None
    with Session() as session:
        return read_secret(name,session)

def write_secret(db,name,value):
    item=db.get(IntegrationSecret,name)
    if item is None:
        item=IntegrationSecret(name=name);db.add(item)
    item.cipher=seal(value)

def connection_status():
    client=read_secret('youtube_client');account=read_secret('youtube_account')
    return {'configured':bool(client),'connected':bool(account),'channel_id':account.get('channel_id') if account else None,
            'channel_title':account.get('channel_title') if account else None,
            'redirect_uri':redirect_uri(),'connect_url':base_url()+'/api/youtube/connect'}

def configure_client(document):
    web=document.get('web')
    if not isinstance(web,dict) or not web.get('client_id','').endswith('.apps.googleusercontent.com') or not web.get('client_secret'):
        raise ValueError('Import the Google OAuth client JSON for a Web application')
    if redirect_uri() not in web.get('redirect_uris',[]):
        raise ValueError('Add this Authorized redirect URI in Google Cloud, then download the JSON again: '+redirect_uri())
    if read_secret('youtube_account'):
        raise ValueError('Disconnect the current channel before changing OAuth clients')
    with Session.begin() as db:
        write_secret(db,'youtube_client',{'client_id':web['client_id'],'client_secret':web['client_secret']})

def begin_connection():
    client=read_secret('youtube_client')
    if not client:
        raise ValueError('Set up the Google OAuth client first')
    state=secrets.token_urlsafe(32);browser=secrets.token_urlsafe(32);verifier=secrets.token_urlsafe(64)
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    with Session.begin() as db:
        db.execute(delete(OAuthAttempt).where(OAuthAttempt.expires<datetime.now(timezone.utc)))
        db.add(OAuthAttempt(state_hash=hashlib.sha256(state.encode()).hexdigest(),browser_hash=hashlib.sha256(browser.encode()).hexdigest(),verifier_cipher=seal(verifier),expires=datetime.now(timezone.utc)+timedelta(minutes=10)))
    params={'client_id':client['client_id'],'redirect_uri':redirect_uri(),'response_type':'code','scope':SCOPES,
            'access_type':'offline','prompt':'consent select_account','state':state,'code_challenge':challenge,'code_challenge_method':'S256'}
    return 'https://accounts.google.com/o/oauth2/v2/auth?'+urlencode(params),browser

def finish_connection(code,state,browser):
    key=hashlib.sha256(state.encode()).hexdigest()
    with Session.begin() as db:
        attempt=db.scalar(select(OAuthAttempt).where(OAuthAttempt.state_hash==key).with_for_update())
        if not attempt or not browser:
            raise ValueError('Connection expired. Start Connect YouTube again.')
        expires=attempt.expires if attempt.expires.tzinfo else attempt.expires.replace(tzinfo=timezone.utc)
        if expires<datetime.now(timezone.utc) or not hmac.compare_digest(attempt.browser_hash,hashlib.sha256(browser.encode()).hexdigest()):
            raise ValueError('Connection state did not match this browser. Start again.')
        verifier=unseal(attempt.verifier_cipher)
        db.delete(attempt)
    client=read_secret('youtube_client')
    with httpx.Client(timeout=30) as http:
        response=http.post(TOKEN_URL,data={**client,'code':code,'grant_type':'authorization_code','redirect_uri':redirect_uri(),'code_verifier':verifier})
        if response.status_code!=200:
            raise ValueError('Google could not complete authorization. Start Connect YouTube again.')
        token=response.json()
        if not token.get('access_token') or not token.get('refresh_token'):
            raise ValueError('Google did not provide offline access. Reconnect and approve the requested permissions.')
        granted=set(token.get('scope',SCOPES).split())
        if not set(SCOPES.split()).issubset(granted):
            raise ValueError('Approve both YouTube upload and channel read access to connect.')
        channel=http.get('https://www.googleapis.com/youtube/v3/channels',params={'part':'snippet','mine':'true'},headers={'Authorization':'Bearer '+token['access_token']})
        if channel.status_code!=200 or not channel.json().get('items'):
            raise ValueError('No YouTube channel was found for this account. Enable YouTube Data API v3 and connect an account with a channel.')
        item=channel.json()['items'][0]
    account={**token,'expires_at':datetime.now(timezone.utc).timestamp()+int(token.get('expires_in',3600)),
             'channel_id':item['id'],'channel_title':item['snippet']['title']}
    with Session.begin() as db:
        write_secret(db,'youtube_account',account)
    return connection_status()

def access_token(channel_id,force=False):
    with Session.begin() as db:
        row=db.scalar(select(IntegrationSecret).where(IntegrationSecret.name=='youtube_account').with_for_update())
        if not row:
            raise ValueError('Connect your YouTube channel to upload')
        account=unseal(row.cipher)
        if account['channel_id']!=channel_id:
            raise ValueError('Reconnect the original channel for this queued upload')
        if not force and account.get('expires_at',0)>datetime.now(timezone.utc).timestamp()+120:
            return account['access_token']
        client=read_secret('youtube_client',db)
        with httpx.Client(timeout=30) as http:
            response=http.post(TOKEN_URL,data={**client,'refresh_token':account['refresh_token'],'grant_type':'refresh_token'})
        if response.status_code!=200:
            raise ValueError('Google authorization expired or was revoked. Reconnect your channel.')
        fresh=response.json()
        account.update(fresh)
        account['expires_at']=datetime.now(timezone.utc).timestamp()+int(fresh.get('expires_in',3600))
        row.cipher=seal(account)
        return account['access_token']

def disconnect():
    account=read_secret('youtube_account')
    if account:
        try:
            with httpx.Client(timeout=15) as http:
                http.post('https://oauth2.googleapis.com/revoke',data={'token':account['refresh_token']})
        except httpx.HTTPError:
            pass
    with Session.begin() as db:
        db.execute(delete(IntegrationSecret).where(IntegrationSecret.name=='youtube_account'))
