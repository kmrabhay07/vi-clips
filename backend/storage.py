"""Leave room for PostgreSQL and the OS before accepting large media writes."""
import shutil

RESERVE = 1024 ** 3

class StorageFull(RuntimeError):
    pass

def ensure_space(directory, additional=0):
    available = shutil.disk_usage(directory).free
    required = max(0, int(additional)) + RESERVE
    if available < required:
        raise StorageFull(f'Not enough working space: need {required/1024**3:.1f} GiB free, '
                          f'but only {available/1024**3:.1f} GiB is available. '
                          'Free storage or import a smaller recording, then retry.')
