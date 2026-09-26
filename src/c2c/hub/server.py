"""c2c hub websockets server: authenticate hosts, drain and route envelopes.

The hub is a relay. It never constructs a typed envelope; it only stores and
forwards envelopes received from authenticated hosts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable

import websockets

from c2c import envelope as _env
from c2c.envelope import MAX_BYTES, EnvelopeError

log = logging.getLogger("c2c.hub.server")

# Max time to wait for the initial `hello` frame before dropping an
# unauthenticated connection. Per-IP connection/rate limiting is NOT done
# here; it is expected to be handled by the deployment's reverse proxy
# (e.g. nginx/haproxy) sitting in front of the hub.
HELLO_TIMEOUT_S = 10


def _now_ms() -> int:
    return int(time.time() * 1000)


class _Conn:
    __slots__ = ("ws", "host", "projects")

    def __init__(self, ws, host: str, projects: set[str]) -> None:
        self.ws = ws
        self.host = host
        self.projects = projects


class Hub:
    def __init__(self, mailbox, auth, now_ms: Callable[[], int] = _now_ms) -> None:
        self._mb = mailbox
        self._auth = auth
        self._now = now_ms
        self._conns: dict[str, _Conn] = {}  # host_id -> _Conn (one per host)

    async def serve(self, host: str, port: int, ssl_context=None):
        # No per-IP connection/rate limiting here by design; that belongs to
        # whatever reverse proxy terminates the connection in front of us.
        return await websockets.serve(
            self.handler, host, port, ssl=ssl_context, max_size=MAX_BYTES,
        )

    async def handler(self, ws) -> None:
        conn = await self._do_hello(ws)
        if conn is None:
            return
        self._conns[conn.host] = conn
        try:
            await self._drain(conn)
            async for raw in ws:
                await self._on_message(conn, raw)
        except websockets.ConnectionClosed:
            pass
        finally:
            if self._conns.get(conn.host) is conn:
                del self._conns[conn.host]

    async def _do_hello(self, ws) -> _Conn | None:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=HELLO_TIMEOUT_S)
            msg = json.loads(raw)
        except asyncio.TimeoutError:
            # No hello within the grace period: drop the connection quietly,
            # no error frame owed to a client that never authenticated.
            try:
                await ws.close()
            except websockets.ConnectionClosed:
                pass
            return None
        except (websockets.ConnectionClosed, ValueError, TypeError, UnicodeDecodeError):
            return None
        if not isinstance(msg, dict) or msg.get("op") != "hello":
            await self._error(ws, "expected hello")
            return None
        token = msg.get("token")
        if not isinstance(token, str) or not token:
            await self._error(ws, "token must be a non-empty string")
            return None
        raw_projects = msg.get("projects", [])
        if not isinstance(raw_projects, list):
            await self._error(ws, "projects must be a list")
            return None
        host = self._auth.verify(token)
        if host is None:
            await self._error(ws, "auth failed")
            return None
        try:
            projects = {p for p in raw_projects if isinstance(p, str)}
        except TypeError:
            projects = set()
        await ws.send(json.dumps({"op": "welcome", "host": host}))
        return _Conn(ws, host, projects)

    async def _drain(self, conn: _Conn) -> None:
        for env in self._mb.pending_for(conn.host, conn.projects, self._now()):
            await conn.ws.send(json.dumps({"op": "deliver", "env": env}))

    async def _on_message(self, conn: _Conn, raw) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        op = msg.get("op")
        if op == "post":
            await self._on_post(conn, msg)
        elif op == "ack":
            mid = msg.get("msg_id")
            if isinstance(mid, str) and mid:
                self._mb.ack(mid, conn.host)
        elif op == "announce":
            raw_projects = msg.get("projects")
            if isinstance(raw_projects, list):
                conn.projects = {p for p in raw_projects if isinstance(p, str)}
            else:
                conn.projects = set()
            await self._drain(conn)

    async def _on_post(self, conn: _Conn, msg: dict) -> None:
        env = msg.get("env")
        try:
            _env.validate(env)
        except EnvelopeError as exc:
            await conn.ws.send(json.dumps({"op": "error", "reason": str(exc)}))
            return
        self._mb.put(env)
        for rc in self._recipients(env):
            try:
                await rc.ws.send(json.dumps({"op": "deliver", "env": env}))
            except websockets.ConnectionClosed:
                pass
        await self._maybe_status(conn, env)

    async def _maybe_status(self, conn: _Conn, env: dict) -> None:
        """Tell the poster when nothing on the far side can take this yet.

        The sending host can't distinguish "peer is down" from "peer is busy"
        -- only the hub sees both the connection table and what each host
        announced. Advisory only: the message is stored either way, and the
        receiving ferry's own view of its sessions is authoritative (announces
        lag by up to the announce interval, so a session that just started may
        not be reflected here yet).
        """
        t = env["target"]
        if t["kind"] != "host":
            return  # fan-out has no single expected recipient to report on
        peer = self._conns.get(t["host"])
        if peer is None:
            state = "held_no_host"
        elif env["project"] not in peer.projects:
            state = "held_no_project"
        else:
            return  # deliverable: a status per successful post is pure noise
        try:
            await conn.ws.send(json.dumps({
                "op": "status", "msg_id": env["msg_id"], "state": state,
                "host": t["host"], "project": env["project"],
            }))
        except websockets.ConnectionClosed:
            pass

    def _recipients(self, env: dict) -> list[_Conn]:
        t = env["target"]
        if t["kind"] == "host":
            rc = self._conns.get(t["host"])
            return [rc] if rc is not None else []
        return [
            c for c in self._conns.values()
            if c.host != env["origin_host"] and env["project"] in c.projects
        ]

    async def _error(self, ws, reason: str) -> None:
        try:
            await ws.send(json.dumps({"op": "error", "reason": reason}))
            await ws.close()
        except websockets.ConnectionClosed:
            pass
