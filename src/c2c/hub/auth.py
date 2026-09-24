"""Per-host bearer-token auth for the hub. Single trust domain."""
from __future__ import annotations

import hmac
import json
import logging
import os
import stat

log = logging.getLogger("c2c.hub.auth")


class Auth:
    def __init__(self, tokens: dict[str, str]) -> None:
        # host_id -> token
        self._tokens = dict(tokens)

    def verify(self, token: str) -> str | None:
        if not token:
            return None
        for host_id, known in self._tokens.items():
            if hmac.compare_digest(token, known):
                return host_id
        return None


def load_auth(path: str) -> Auth:
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode != 0o600:
        log.warning("hub auth file %s is mode %o, expected 600", path, mode)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    hosts = data.get("hosts")
    if not isinstance(hosts, dict) or not hosts:
        raise ValueError("config must have a non-empty 'hosts' object")
    for host_id, token in hosts.items():
        if not isinstance(token, str) or not token:
            raise ValueError(f"empty token for host {host_id!r}")
    return Auth(hosts)
