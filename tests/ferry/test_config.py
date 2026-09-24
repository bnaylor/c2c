import json
import os
import pytest
from c2c.ferry.config import load_config


def test_load_config_ok(tmp_path):
    p = tmp_path / "ferry.json"
    p.write_text(json.dumps({
        "host_id": "home", "peer_host": "work",
        "hub_url": "wss://hub.example:8765", "token": "T",
    }))
    os.chmod(p, 0o600)
    c = load_config(str(p))
    assert c.host_id == "home" and c.peer_host == "work"
    assert c.peer_name == "work"
    assert c.dedup_window_ms == 600000


def test_load_config_missing_field(tmp_path):
    p = tmp_path / "ferry.json"
    p.write_text(json.dumps({"host_id": "home"}))
    os.chmod(p, 0o600)
    with pytest.raises(ValueError):
        load_config(str(p))
