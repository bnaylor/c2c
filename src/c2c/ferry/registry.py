"""Read the local Claude session registry and pick delivery targets."""
from __future__ import annotations

import hashlib
import json
import os
import re

from c2c.ferry import wire

_PID_JSON = re.compile(r"^\d+\.json$")


def pid_alive(pid) -> bool:
    """Whether a process with this pid currently exists.

    Claude Code checks this too, and also compares the entry's `procStart`
    against the process's real start time. We deliberately skip that: it means
    formatting and parsing a date string per candidate, and a recycled pid
    fails at connect time anyway -- nothing else is listening on the dead
    session's socket path. on_deliver falls through to the next candidate when
    an inject fails, which covers that case without the brittleness.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    except (OSError, TypeError, ValueError, OverflowError):
        return False
    return True


def read_sessions(sessions_dir: str, is_alive=None) -> list[dict]:
    """Live sessions in the registry.

    Entries whose process is gone are skipped: a session killed without a
    chance to clean up leaves its entry, key file and socket behind, and a
    stale entry has the freshest statusUpdatedAt of anything on its project --
    so it wins target selection and then refuses the connection.
    """
    if is_alive is None:
        is_alive = pid_alive  # resolved per call, so it stays substitutable
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
                entry = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(entry, dict) or not is_alive(entry.get("pid")):
            continue
        out.append(entry)
    return out


def session_by_socket(sessions: list[dict], sock: str) -> dict | None:
    path = sock[4:] if sock.startswith("uds:") else sock
    for s in sessions:
        if s.get("messagingSocketPath") == path:
            return s
    return None


def rank_targets(sessions, project, projectfn) -> list[dict]:
    """Sessions that could take a message for this project, best first.

    Background agents first -- they sit alive taking turns, which is what an
    unsolicited request wants -- then interactive ones, each group most
    recently active first. Returning the whole ranking rather than one winner
    lets a caller fall through when the best candidate refuses the connection.
    """
    matches = [s for s in sessions if projectfn(s.get("cwd", "")) == project]
    matches = [s for s in matches if wire.protocol_ok(s)]
    matches.sort(key=lambda s: s.get("statusUpdatedAt", 0), reverse=True)
    return ([s for s in matches if s.get("kind") == "bg"]
            + [s for s in matches if s.get("kind") != "bg"])


def pick_target(sessions, project, projectfn) -> dict | None:
    ranked = rank_targets(sessions, project, projectfn)
    return ranked[0] if ranked else None


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


def session_by_id(sessions, session_id: str) -> dict | None:
    """Exact-match lookup by the registry's stable `sessionId`.

    Used for reply routing, where the one session that asked the question is
    the only correct target -- unlike pick_target's bg-preferring heuristic,
    which is for unsolicited work.
    """
    for s in sessions:
        if s.get("sessionId") == session_id and wire.protocol_ok(s):
            return s
    return None
