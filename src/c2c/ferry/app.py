"""Ferry orchestrator: glue delivery, local relay, and reply correlation."""
from __future__ import annotations

import logging
import time
import uuid
from typing import Callable

from c2c.ferry import projectid, registry

log = logging.getLogger("c2c.ferry.app")


def _now_ms() -> int:
    return int(time.time() * 1000)


class Ferry:
    def __init__(self, config, local_peer, hub_client, dedup,
                 projectfn: Callable[[str], str | None] = projectid.project_for_cwd,
                 now_ms: Callable[[], int] = _now_ms) -> None:
        self._cfg = config
        self._peer = local_peer
        self._hub = hub_client
        self._dedup = dedup
        self._projectfn = projectfn
        self._now = now_ms
        # sender socket -> orig msg_id awaiting a reply
        self._pending_reply: dict[str, str] = {}

    def _sessions(self) -> list[dict]:
        return [
            s for s in registry.read_sessions(self._cfg.sessions_dir)
            if s.get("pid") != self._peer.pid  # exclude our own peer entry
        ]

    def live_projects(self) -> list[str]:
        out = set()
        for s in self._sessions():
            pid = self._projectfn(s.get("cwd", ""))
            if pid:
                out.add(pid)
        return sorted(out)

    async def on_deliver(self, env: dict) -> None:
        mid = env["msg_id"]
        if self._dedup.seen(mid):
            self._hub.ack(mid)
            return
        target = registry.pick_target(self._sessions(), env["project"], self._projectfn)
        if target is None:
            log.info("no local session for project %s; holding %s", env["project"], mid)
            return  # do NOT ack: redelivers later
        payload = env.get("payload", {})
        # Record the reply-correlation BEFORE injecting: inject() writes to
        # the target and can trigger a reply before it returns (the target
        # may reply as soon as bytes hit its socket, well before inject()'s
        # own post-write bookkeeping would run). If we recorded this after
        # inject() returned, a fast reply could race ahead and land
        # misclassified as a plain "note" instead of a "reply".
        self._pending_reply[target["messagingSocketPath"]] = mid
        ok = await self._peer.inject(
            target, payload.get("body", ""), payload.get("hop_chain"),
            str(uuid.uuid4()),
        )
        if not ok:
            log.warning("inject failed for %s -> %s", mid, target.get("name"))
            self._pending_reply.pop(target["messagingSocketPath"], None)
            return  # do NOT ack
        self._hub.ack(mid)

    def on_local_message(self, fields: dict) -> None:
        sender = registry.session_by_socket(self._sessions(), fields.get("raw_from", ""))
        if sender is None:
            log.info("local message from unknown socket %s; dropping",
                     fields.get("raw_from"))
            return
        project = self._projectfn(sender.get("cwd", ""))
        if not project:
            log.info("sender cwd %s has no project; dropping", sender.get("cwd"))
            return
        sock_path = sender["messagingSocketPath"]
        orig = self._pending_reply.pop(sock_path, None)
        mtype = "reply" if orig else "note"
        env = self.build_envelope(project, fields.get("body", ""),
                                  fields.get("hop_chain"), mtype, orig)
        self._hub.post(env)

    def build_envelope(self, project, body, hop_chain, mtype, orig_msg_id) -> dict:
        return {
            "v": 1,
            "msg_id": str(uuid.uuid4()),
            "origin_host": self._cfg.host_id,
            "project": project,
            "target": {"kind": "host", "host": self._cfg.peer_host, "project": project},
            "type": mtype,
            "ttl_s": 604800,
            "created_at": self._now(),
            "orig_msg_id": orig_msg_id,
            "payload": {"body": body, "hop_chain": hop_chain,
                        "from_name": self._cfg.host_id},
        }
