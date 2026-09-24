"""c2c hub websockets server: authenticate hosts, drain and route envelopes.

The hub is a relay. It never constructs a typed envelope; it only stores and
forwards envelopes received from authenticated hosts.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Callable

import websockets

from c2c import envelope as _env
from c2c.envelope import MAX_BYTES, EnvelopeError

log = logging.getLogger("c2c.hub.server")


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
            raw = await ws.recv()
            msg = json.loads(raw)
        except (websockets.ConnectionClosed, ValueError):
            return None
        if not isinstance(msg, dict) or msg.get("op") != "hello":
            await self._error(ws, "expected hello")
            return None
        host = self._auth.verify(msg.get("token", ""))
        if host is None:
            await self._error(ws, "auth failed")
            return None
        projects = set(msg.get("projects") or [])
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
            conn.projects = set(msg.get("projects") or [])
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
