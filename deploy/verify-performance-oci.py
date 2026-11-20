"""Check optimizations and concurrent claims against our own disposable database."""
import os
import sys
import uuid
import unittest
from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url

url=make_url(os.environ['DATABASE_URL'])
name='vi_perf_check_'+uuid.uuid4().hex
assert url.database!=name
admin=create_engine(url,isolation_level='AUTOCOMMIT')
with admin.connect() as connection:
    connection.execute(text(f'CREATE DATABASE {name}'))
try:
    os.environ['TEST_DATABASE_URL']=url.set(database=name).render_as_string(hide_password=False)
    sys.path[:0]=['/app','/app/tests']
    suite=unittest.TestSuite()
    for pattern in ('test_optimizations.py','test_parallel.py','test_youtube.py'):
        suite.addTests(unittest.defaultTestLoader.discover('/app/tests',pattern=pattern))
    from db import engine,DATA
    assert engine.url.database==name and str(DATA).startswith('/tmp/vi-clips-tests-')
    result=unittest.TextTestRunner(verbosity=1).run(suite)
    engine.dispose()
finally:
    # Only the random test database created above is removed.
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE {name} WITH (FORCE)'))
    admin.dispose()
if not result.wasSuccessful():
    raise SystemExit(1)
