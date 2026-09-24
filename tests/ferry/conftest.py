"""Shared fixtures for tests/ferry/."""
import shutil
import tempfile

import pytest


@pytest.fixture
def short_sock_dir():
    """A short-path directory under /tmp for AF_UNIX socket files.

    macOS caps sockaddr_un.sun_path at ~104 bytes. Pytest's default tmp_path
    (nested under /var/folders/.../pytest-of-<user>/pytest-<n>/<test>) is
    frequently longer than that, so AF_UNIX bind()/connect() fail there
    regardless of TMPDIR. Socket files must live under a short, known-safe
    base directory; registry/key JSON files have no such length constraint
    and may still use tmp_path.
    """
    d = tempfile.mkdtemp(prefix="c2c-", dir="/tmp")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)
