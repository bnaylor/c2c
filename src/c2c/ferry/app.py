"""Ferry orchestrator: glue delivery, local relay, and reply correlation."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from typing import Callable

from c2c.ferry import projectid, registry

log = logging.getLogger("c2c.ferry.app")

# Max notes outstanding to one local session before the oldest stops being
# correlatable. Bounds memory; an evicted entry only costs that answer its
# "reply" classification (it posts as a plain note), never the message itself.
PENDING_PER_SOCKET_MAX = 16

# Max unanswered notes we keep a reply-pin for. One entry per note sent, only
# drained when its answer arrives, so an unbounded map would leak for every
# note that never gets answered.
AWAITING_REPLY_MAX = 1000

# Envelope lifetimes. A note is addressed to a project, so any session there
# can eventually take it -- a week of hub redelivery is useful. A reply is
# addressed to one session; once that session is gone for good, redelivering
# for a week accomplishes nothing.
NOTE_TTL_S = 604800
REPLY_TTL_S = 3600

# Don't re-tell one session the same thing inside this window. Five messages
# fired at an offline host should produce one heads-up, not five -- and the
# receiving inbox drops an identical body re-sent within 30s anyway, so
# uncoalesced repeats would silently vanish and look broken.
NOTIFY_COOLDOWN_MS = 60000


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
        # sender socket -> FIFO of orig msg_ids awaiting a reply. A queue, not
        # a single slot: two notes can be outstanding to one session, and the
        # answers come back in the order they were asked.
        self._pending_reply: dict[str, deque[str]] = {}
        # msg_id we posted -> sessionId of the local session that asked, so its
        # reply comes back to it instead of to whatever pick_target prefers.
        self._awaiting_reply: dict[str, str] = {}
        # (sessionId, kind of news) -> when we last said it, for coalescing.
        self._notified: dict[tuple[str, str], int] = {}
        # strong refs for fire-and-forget notifies; a bare create_task can be
        # garbage-collected mid-flight.
        self._notify_tasks: set = set()

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
        if self._dedup.check(mid):
            # already delivered -> this is a genuine duplicate deliver;
            # suppress it and re-ack so the hub stops redelivering.
            self._hub.ack(mid)
            return
        target, pin = self._select_target(env, mid)
        if target is None:
            return  # do NOT record, do NOT ack: redelivers later
        payload = env.get("payload", {})
        # Record the reply-correlation BEFORE injecting: inject() writes to
        # the target and can trigger a reply before it returns (the target
        # may reply as soon as bytes hit its socket, well before inject()'s
        # own post-write bookkeeping would run). If we recorded this after
        # inject() returned, a fast reply could race ahead and land
        # misclassified as a plain "note" instead of a "reply".
        sock_path = target["messagingSocketPath"]
        self._pending_reply.setdefault(
            sock_path, deque(maxlen=PENDING_PER_SOCKET_MAX)).append(mid)
        ok = await self._peer.inject(
            target, payload.get("body", ""), payload.get("hop_chain"),
            str(uuid.uuid4()),
        )
        if not ok:
            log.warning("inject failed for %s -> %s", mid, target.get("name"))
            self._unqueue_pending(sock_path, mid)
            return  # do NOT record, do NOT ack
        # Only mark as delivered once inject has actually succeeded, so a
        # held or failed delivery remains eligible for a later redeliver
        # instead of being silently dropped as a "duplicate".
        self._dedup.record(mid)
        # Drop the pin only now: a held or failed reply must keep it, or the
        # redelivery would fall through to pick_target and land in the wrong
        # session -- the exact bug the pin exists to prevent.
        if pin is not None:
            self._awaiting_reply.pop(pin, None)
        self._hub.ack(mid)

    def on_local_message(self, fields: dict) -> None:
        sender = registry.session_by_socket(self._sessions(), fields.get("raw_from", ""))
        if sender is None:
            log.info("local message from unknown socket %s; dropping",
                     fields.get("raw_from"))
            return
        project = self._projectfn(sender.get("cwd", ""))
        if not project:
            cwd = sender.get("cwd", "")
            log.info("sender cwd %s has no project; dropping", cwd)
            sid = sender.get("sessionId")
            if sid:
                self._spawn_notify(sid, "no_project", (
                    f"[c2c] Not sent: {cwd} has no git 'origin', so there is no "
                    f"project to route to. c2c keys projects on the origin URL "
                    f"-- send from a checkout that has one."))
            return
        sock_path = sender["messagingSocketPath"]
        orig = self._next_pending(sock_path)
        mtype = "reply" if orig else "note"
        env = self.build_envelope(project, fields.get("body", ""),
                                  fields.get("hop_chain"), mtype, orig)
        # Pin every outbound message, not just notes. A follow-up from this
        # session posts as a `reply` (it answers the inbound one), and the
        # answer to that follow-up has to come back here too, or a multi-turn
        # exchange jumps to another session after the first round trip.
        sid = sender.get("sessionId")
        if sid:
            self._pin_reply(env["msg_id"], sid)
        self._hub.post(env)

    async def on_hub_status(self, msg: dict) -> None:
        """The hub says this message can't be delivered yet. Tell whoever asked.

        Advisory: the hub stores the message regardless, and the far ferry's
        own view of its sessions is authoritative. So the wording promises
        eventual delivery rather than claiming the far side is empty -- which
        would be wrong whenever an announce is merely stale.
        """
        sid = self._awaiting_reply.get(msg.get("msg_id"))
        if sid is None:
            return  # not ours to report (restarted ferry, or someone else's)
        body = self._status_body(msg)
        if body is None:
            return
        await self._notify_session(sid, f"{msg.get('state')}", body)

    def _spawn_notify(self, session_id: str, key: str, body: str) -> None:
        """Schedule a notify from a sync caller (the local-message callback).

        Fire-and-forget deliberately: handling an inbound peer frame should not
        block on a courtesy note, and a failed one is logged rather than
        retried.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop to schedule on (direct call outside the ferry)
        task = loop.create_task(self._notify_session(session_id, key, body))
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)

    def _status_body(self, msg: dict) -> str | None:
        host, project = msg.get("host"), msg.get("project")
        state = msg.get("state")
        if state == "held_no_host":
            return (f"[c2c] Held: {host} isn't connected right now. Your "
                    f"message is queued and will be delivered when it is.")
        if state == "held_no_project":
            return (f"[c2c] Held: nothing on {host} is running {project} right "
                    f"now. Your message is queued and will be delivered once "
                    f"something is.")
        return None  # unknown state: say nothing rather than guess

    async def _notify_session(self, session_id: str, key: str, body: str) -> None:
        """Inject a ferry-authored note into one local session.

        Goes straight to inject(), never through on_deliver(): the
        pending-reply correlation belongs to real peer messages, and recording
        this would make the session's next message look like a reply to a note
        the ferry invented.
        """
        target = registry.session_by_id(self._sessions(), session_id)
        if target is None:
            return
        if not self._notify_ok(session_id, key):
            return
        ok = await self._peer.inject(target, body, None, str(uuid.uuid4()))
        if not ok:
            log.warning("could not notify %s: %s", target.get("name"), body)

    def _notify_ok(self, session_id: str, key: str) -> bool:
        now = self._now()
        k = (session_id, key)
        last = self._notified.get(k)
        if last is not None and now - last < NOTIFY_COOLDOWN_MS:
            return False
        self._notified = {
            kk: t for kk, t in self._notified.items()
            if now - t < NOTIFY_COOLDOWN_MS
        }
        self._notified[k] = now
        return True

    def _pin_reply(self, msg_id: str, session_id: str) -> None:
        self._awaiting_reply[msg_id] = session_id
        # dict preserves insertion order, so the front is the oldest pin.
        while len(self._awaiting_reply) > AWAITING_REPLY_MAX:
            self._awaiting_reply.pop(next(iter(self._awaiting_reply)))

    def _next_pending(self, sock_path: str) -> str | None:
        """Oldest msg_id still awaiting an answer from this session."""
        q = self._pending_reply.get(sock_path)
        if not q:
            return None
        orig = q.popleft()
        if not q:
            del self._pending_reply[sock_path]
        return orig

    def _unqueue_pending(self, sock_path: str, mid: str) -> None:
        q = self._pending_reply.get(sock_path)
        if q is None:
            return
        try:
            q.remove(mid)
        except ValueError:
            pass
        if not q:
            del self._pending_reply[sock_path]

    def _select_target(self, env: dict, mid: str) -> tuple[dict | None, str | None]:
        """The session to inject into, and the reply-pin to retire once that
        inject succeeds. A None target means hold for a later redelivery."""
        pin = self._reply_pin(env)
        if pin is not None:
            # A reply belongs to the session that asked, so pick_target's bg
            # preference must not get a vote here. If that session is gone we
            # hold rather than hand the answer to a session that never asked.
            target = registry.session_by_id(self._sessions(),
                                            self._awaiting_reply[pin])
            if target is None:
                log.info("origin session for %s is gone; holding reply %s", pin, mid)
            return target, pin
        target = registry.pick_target(self._sessions(), env["project"],
                                      self._projectfn)
        if target is None:
            log.info("no local session for project %s; holding %s",
                     env["project"], mid)
        return target, None

    def _reply_pin(self, env: dict) -> str | None:
        """The orig msg_id this reply answers, if we are the host that asked."""
        if env.get("type") != "reply":
            return None
        orig = env.get("orig_msg_id")
        return orig if orig in self._awaiting_reply else None

    def build_envelope(self, project, body, hop_chain, mtype, orig_msg_id) -> dict:
        return {
            "v": 1,
            "msg_id": str(uuid.uuid4()),
            "origin_host": self._cfg.host_id,
            "project": project,
            "target": {"kind": "host", "host": self._cfg.peer_host, "project": project},
            "type": mtype,
            "ttl_s": REPLY_TTL_S if mtype == "reply" else NOTE_TTL_S,
            "created_at": self._now(),
            "orig_msg_id": orig_msg_id,
            "payload": {"body": body, "hop_chain": hop_chain,
                        "from_name": self._cfg.host_id},
        }
