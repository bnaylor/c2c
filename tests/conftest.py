"""Suite-wide fixtures."""
import pytest

from c2c.ferry import registry


@pytest.fixture(autouse=True)
def synthetic_pids_count_as_live(monkeypatch, request):
    """Let tests keep inventing pids.

    Registry fixtures throughout the suite write entries for pids like 300 or
    1100 that no process owns, and read_sessions now drops entries whose
    process is gone -- so without this every one of them reads as an empty
    registry. Tests that exercise liveness itself pass `is_alive` explicitly or
    carry the `real_pid_liveness` marker.
    """
    if "real_pid_liveness" in request.keywords:
        return
    monkeypatch.setattr(registry, "pid_alive", lambda pid: True)
