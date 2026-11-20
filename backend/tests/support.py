import atexit
import os
import tempfile
from pathlib import Path

scratch = tempfile.TemporaryDirectory(prefix='vi-clips-tests-')
os.environ['DATA_DIR'] = scratch.name
os.environ['DATABASE_URL'] = os.getenv('TEST_DATABASE_URL', 'sqlite:///' + str(Path(scratch.name) / 'test.db'))

def cleanup():
    from db import engine
    engine.dispose()
    scratch.cleanup()

atexit.register(cleanup)
