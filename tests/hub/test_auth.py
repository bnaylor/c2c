import json
import os
import pytest
from c2c.hub.auth import Auth, load_auth


def test_verify_returns_host_for_valid_token():
    a = Auth({"home": "tok-home", "work": "tok-work"})
    assert a.verify("tok-work") == "work"
    assert a.verify("tok-home") == "home"


def test_verify_returns_none_for_bad_token():
    a = Auth({"home": "tok-home"})
    assert a.verify("nope") is None
    assert a.verify("") is None


def test_load_auth_reads_file(tmp_path):
    p = tmp_path / "hub.json"
    p.write_text(json.dumps({"hosts": {"home": "t1", "work": "t2"}}))
    os.chmod(p, 0o600)
    a = load_auth(str(p))
    assert a.verify("t2") == "work"


def test_load_auth_rejects_empty_token(tmp_path):
    p = tmp_path / "hub.json"
    p.write_text(json.dumps({"hosts": {"home": ""}}))
    os.chmod(p, 0o600)
    with pytest.raises(ValueError):
        load_auth(str(p))


def test_load_auth_rejects_missing_hosts(tmp_path):
    p = tmp_path / "hub.json"
    p.write_text(json.dumps({"nope": {}}))
    os.chmod(p, 0o600)
    with pytest.raises(ValueError):
        load_auth(str(p))
