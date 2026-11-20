"""Exercise cleanup against a disposable PostgreSQL database, never the live one."""
import os
import sys
import uuid
import unittest
from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url

url=make_url(os.environ['DATABASE_URL'])
name='vi_cleanup_check_'+uuid.uuid4().hex
admin=create_engine(url,isolation_level='AUTOCOMMIT')
with admin.connect() as connection:
    connection.execute(text(f'CREATE DATABASE {name}'))
try:
    os.environ['TEST_DATABASE_URL']=url.set(database=name).render_as_string(hide_password=False)
    sys.path.insert(0,'/app')
    sys.path.insert(0,'/app/tests')
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover('/app/tests',pattern='test_published_cleanup.py'))
    from db import engine
    engine.dispose()
finally:
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE {name} WITH (FORCE)'))
    admin.dispose()
if not result.wasSuccessful():
    raise SystemExit(1)
