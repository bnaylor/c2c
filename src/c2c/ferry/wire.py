"""Adapter for Claude Code's cross-session peer frames (protocol v1).

Everything that constructs or parses a peer frame lives here so a protocol
bump is contained to one file. See docs/protocol.md for the captured ground
truth these functions reproduce byte-for-byte.
"""
from __future__ import annotations

import json
import re

PROTOCOL_VERSION = 1

_TAG = "cross-session-message"
# attribute order MUST be: from, [from-session], [hop-chain], from-name, from-mode
_RE = re.compile(
    r'^<' + _TAG + r'(?: from="([^"]*)")?'
    r'(?: from-session="([^"]*)")?'
    r'(?: hop-chain="([^"]*)")?'
    r'(?: from-name="([^"]*)")?'
    r'(?: from-mode="([^"]*)")?>\n'
    r'([\s\S]*)\n</' + _TAG + r'>$'
)


def build_wrapper(from_uri: str, from_name: str, body: str,
                  hop_chain: list[str] | None = None,
                  from_mode: str = "prompting") -> str:
    attrs = [f'from="{from_uri}"']
    if hop_chain:
        attrs.append(f'hop-chain="{",".join(hop_chain)}"')
    attrs.append(f'from-name="{from_name}"')
    attrs.append(f'from-mode="{from_mode}"')
    return f"<{_TAG} {' '.join(attrs)}>\n{body}\n</{_TAG}>"


def parse_wrapper(content: str) -> dict | None:
    m = _RE.match(content)
    if not m:
        return None
    frm, _from_session, hop, name, mode, body = m.groups()
    return {
        "from": frm,
        "from_name": name,
        "from_mode": mode,
        "hop_chain": hop.split(",") if hop else None,
        "body": body,
    }


def build_user_message(from_uri: str, from_name: str, body: str,
                       msg_id: str, hop_chain: list[str] | None = None) -> dict:
    return {
        "msgV": 1,
        "msg_id": msg_id,
        "type": "user",
        "message": {
            "role": "user",
            "content": build_wrapper(from_uri, from_name, body, hop_chain),
        },
        "priority": "next",
        "from": from_uri,
    }


def encode_frames(token: str, user_msg: dict) -> bytes:
    auth = json.dumps({"type": "auth", "token": token}, separators=(",", ":"))
    user = json.dumps(user_msg, separators=(",", ":"))
    return (auth + "\n" + user + "\n").encode("utf-8")


def protocol_ok(entry: dict) -> bool:
    return entry.get("peerProtocol") == PROTOCOL_VERSION
