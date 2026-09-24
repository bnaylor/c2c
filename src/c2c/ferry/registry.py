"""Read the local Claude session registry and pick delivery targets."""
from __future__ import annotations

import hashlib
import json
import os
import re

from c2c.ferry import wire

_PID_JSON = re.compile(r"^\d+\.json$")


def read_sessions(sessions_dir: str) -> list[dict]:
    out = []
    try:
        names = os.listdir(sessions_dir)
    except OSError:
        return out
    for fn in names:
        if not _PID_JSON.match(fn):
            continue
        try:
            with open(os.path.join(sessions_dir, fn), "r", encoding="utf-8") as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            continue
    return out


def session_by_socket(sessions: list[dict], sock: str) -> dict | None:
    path = sock[4:] if sock.startswith("uds:") else sock
    for s in sessions:
        if s.get("messagingSocketPath") == path:
            return s
    return None


def pick_target(sessions, project, projectfn) -> dict | None:
    matches = [s for s in sessions if projectfn(s.get("cwd", "")) == project]
    matches = [s for s in matches if wire.protocol_ok(s)]
    if not matches:
        return None
    bg = [s for s in matches if s.get("kind") == "bg"]
    pool = bg or matches
    return max(pool, key=lambda s: s.get("statusUpdatedAt", 0))


def peer_token_for(entry: dict, sessions_dir: str) -> str | None:
    sock = entry.get("messagingSocketPath")
    pid = entry.get("pid")
    if not sock or pid is None:
        return None
    h = hashlib.sha256(sock.encode()).hexdigest()  # no realpath
    key_path = os.path.join(sessions_dir, f"{pid}.{h}.key")
    try:
        with open(key_path, "r", encoding="utf-8") as f:
            return json.load(f).get("peerToken")
    except (OSError, ValueError):
        return None
