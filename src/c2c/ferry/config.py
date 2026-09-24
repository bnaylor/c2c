"""Ferry configuration."""
from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import dataclass

log = logging.getLogger("c2c.ferry.config")


@dataclass
class FerryConfig:
    host_id: str
    peer_host: str
    hub_url: str
    token: str
    sessions_dir: str = os.path.expanduser("~/.claude/sessions")
    sock_dir: str = "/tmp/cc-socks"
    dedup_window_ms: int = 600000
    queue_path: str = os.path.expanduser("~/.c2c/outbound.db")

    @property
    def peer_name(self) -> str:
        return self.peer_host


def load_config(path: str) -> FerryConfig:
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode != 0o600:
        log.warning("ferry config %s is mode %o, expected 600", path, mode)
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    for k in ("host_id", "peer_host", "hub_url", "token"):
        if not d.get(k):
            raise ValueError(f"missing config field: {k}")
    known = {f for f in FerryConfig.__dataclass_fields__}
    return FerryConfig(**{k: v for k, v in d.items() if k in known})
