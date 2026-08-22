import os
import sys


LIB_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'lib'))
if LIB_DIR not in sys.path:
    sys.path.insert(0, LIB_DIR)

# Ubuntu 18.04 ships pytest 3.3, which provides ``tmpdir`` but not
# ``tmp_path``.  Keep the test suite runnable there while preserving the
# pathlib.Path interface expected by the tests.
import pathlib
import pytest


@pytest.fixture
def tmp_path(tmpdir):
    return pathlib.Path(str(tmpdir))
