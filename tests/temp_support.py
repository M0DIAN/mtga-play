"""Owned test directories; MTGA_TEST_TMP never silently falls back on failure."""
from contextlib import contextmanager
import os
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp


@contextmanager
def test_directory():
    configured = os.environ.get('MTGA_TEST_TMP')
    if configured == '':
        raise ValueError('MTGA_TEST_TMP is set but empty')
    root = Path(configured if configured is not None else Path.cwd() / '.mtga-test-tmp').resolve()
    root.mkdir(parents=True, exist_ok=True)
    directory = mkdtemp(dir=root, prefix='mtga-test-')
    try:
        yield directory
    finally:
        rmtree(directory)
