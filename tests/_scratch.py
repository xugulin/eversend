"""Where tests put their temporary data.

``/tmp`` is commonly a small tmpfs (7.7 GB here) while the project lives on a
large disk, and these tests write multi-gigabyte files on purpose.  Putting the
scratch space next to the project keeps the tmpfs free for everything else --
filling it makes unrelated tools fail in confusing ways.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

#: Root for test scratch space.  Override with ``EVERSEND_TEST_TMP``.
SCRATCH_ROOT = Path(
    os.environ.get("EVERSEND_TEST_TMP")
    or Path(__file__).resolve().parent.parent / ".testscratch"
)


@contextmanager
def scratch(prefix: str = "run-"):
    """A temporary directory on the big disk, always cleaned up."""
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=str(SCRATCH_ROOT)))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


__all__ = ["SCRATCH_ROOT", "scratch"]
