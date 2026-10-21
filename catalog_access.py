"""Cross-process catalog coordination. Never hold this lock across downloads."""
from contextlib import contextmanager
import fcntl
from pathlib import Path

CANONICAL_DB = Path('/home/livinggaul/.local/state/livinggaul-x-downloader/download_history.sqlite')


def lock_path(db):
    db = Path(db).resolve()
    if db == CANONICAL_DB:
        return Path('/home/livinggaul/runtime.lock')
    return db.with_suffix(db.suffix + '.catalog.lock')


@contextmanager
def file_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
