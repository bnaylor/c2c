"""Coordination envelope: schema shared by hub and ferry.

The hub treats `payload` as opaque; only the metadata fields drive routing.
"""
from __future__ import annotations

import json

KINDS = frozenset(
    {"review_request", "test_request", "assign_issue", "note", "reply"}
)
TARGET_KINDS = frozenset({"project", "host"})
MAX_BYTES = 1_048_576

_REQUIRED = (
    "v", "msg_id", "origin_host", "project", "target",
    "type", "ttl_s", "created_at", "orig_msg_id", "payload",
)


class EnvelopeError(ValueError):
    pass


def validate(d: dict) -> None:
    if not isinstance(d, dict):
        raise EnvelopeError("envelope must be an object")
    for k in _REQUIRED:
        if k not in d:
            raise EnvelopeError(f"missing field: {k}")
    if d["v"] != 1:
        raise EnvelopeError(f"unsupported version: {d['v']!r}")
    for k in ("msg_id", "origin_host", "project", "type"):
        if not isinstance(d[k], str) or not d[k]:
            raise EnvelopeError(f"field {k} must be a non-empty string")
    if d["type"] not in KINDS:
        raise EnvelopeError(f"unknown type: {d['type']!r}")
    if not isinstance(d["ttl_s"], int) or d["ttl_s"] <= 0:
        raise EnvelopeError("ttl_s must be a positive int")
    if not isinstance(d["created_at"], int) or d["created_at"] <= 0:
        raise EnvelopeError("created_at must be a positive int (epoch ms)")
    t = d["target"]
    if not isinstance(t, dict) or t.get("kind") not in TARGET_KINDS:
        raise EnvelopeError("target.kind must be 'project' or 'host'")
    if t["kind"] == "project":
        if not isinstance(t.get("project"), str) or not t["project"]:
            raise EnvelopeError("project target needs a project string")
    else:  # host
        if not isinstance(t.get("host"), str) or not t["host"]:
            raise EnvelopeError("host target needs a host string")
        if not isinstance(t.get("project"), str) or not t["project"]:
            raise EnvelopeError("host target needs a project string")
    if d["type"] == "reply":
        if not isinstance(d["orig_msg_id"], str) or not d["orig_msg_id"]:
            raise EnvelopeError("reply requires orig_msg_id")
    elif d["orig_msg_id"] is not None and not isinstance(d["orig_msg_id"], str):
        raise EnvelopeError("orig_msg_id must be a string or null")


def encode(d: dict) -> bytes:
    validate(d)
    b = json.dumps(d, separators=(",", ":")).encode("utf-8")
    if len(b) > MAX_BYTES:
        raise EnvelopeError(f"envelope too large: {len(b)} > {MAX_BYTES}")
    return b


def decode(b: bytes) -> dict:
    if len(b) > MAX_BYTES:
        raise EnvelopeError(f"frame too large: {len(b)} > {MAX_BYTES}")
    try:
        d = json.loads(b.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise EnvelopeError(f"bad JSON: {exc}") from exc
    validate(d)
    return d


def expires_at(d: dict) -> int:
    return d["created_at"] + d["ttl_s"] * 1000
