import json
import pytest
from c2c import envelope as e


def good() -> dict:
    return {
        "v": 1,
        "msg_id": "11111111-1111-4111-8111-111111111111",
        "origin_host": "home",
        "project": "github.com/sackheads/iris",
        "target": {"kind": "project", "project": "github.com/sackheads/iris"},
        "type": "review_request",
        "ttl_s": 604800,
        "created_at": 1790000000000,
        "orig_msg_id": None,
        "payload": {"pr": 42},
    }


def test_validate_accepts_good():
    e.validate(good())  # no raise


def test_validate_rejects_unknown_type():
    d = good(); d["type"] = "launch_missiles"
    with pytest.raises(e.EnvelopeError):
        e.validate(d)


def test_validate_rejects_missing_field():
    d = good(); del d["origin_host"]
    with pytest.raises(e.EnvelopeError):
        e.validate(d)


def test_validate_rejects_bad_target_kind():
    d = good(); d["target"] = {"kind": "galaxy"}
    with pytest.raises(e.EnvelopeError):
        e.validate(d)


def test_directed_target_requires_host_and_project():
    d = good(); d["target"] = {"kind": "host", "project": d["project"]}
    with pytest.raises(e.EnvelopeError):
        e.validate(d)


def test_reply_requires_orig_msg_id():
    d = good(); d["type"] = "reply"; d["orig_msg_id"] = None
    with pytest.raises(e.EnvelopeError):
        e.validate(d)


def test_encode_decode_roundtrip():
    d = good()
    assert e.decode(e.encode(d)) == d


def test_encode_rejects_oversize():
    d = good(); d["payload"] = {"blob": "x" * (e.MAX_BYTES)}
    with pytest.raises(e.EnvelopeError):
        e.encode(d)


def test_decode_rejects_bad_json():
    with pytest.raises(e.EnvelopeError):
        e.decode(b"{not json")


def test_expires_at():
    d = good()
    assert e.expires_at(d) == 1790000000000 + 604800 * 1000
