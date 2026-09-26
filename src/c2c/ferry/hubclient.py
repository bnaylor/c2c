"""Ferry-side hub client: durable, reconnecting wss link to the hub."""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections import deque
from typing import Callable

import websockets

log = logging.getLogger("c2c.ferry.hubclient")

# Cap on queued-but-unsent ops. Bounds memory during a long hub outage; the
# oldest op is dropped when full (at-least-once + hub redelivery cover posts,
# and stale acks/announces are harmless to lose).
OUTBOX_MAX = 10000


class HubClient:
    def __init__(self, url: str, token: str,
                 projects_provider: Callable[[], list[str]],
                 on_deliver: Callable[[dict], object]) -> None:
        self._url = url
        self._token = token
        self._projects = projects_provider
        self._on_deliver = on_deliver
        self._outbox: deque[dict] = deque(maxlen=OUTBOX_MAX)  # queued ops
        self._ws = None
        self._connected = False
        self._flush_lock = asyncio.Lock()

    def post(self, env: dict) -> None:
        self._enqueue({"op": "post", "env": env})

    def ack(self, msg_id: str) -> None:
        self._enqueue({"op": "ack", "msg_id": msg_id})

    def announce(self, projects: list[str]) -> None:
        self._enqueue({"op": "announce", "projects": projects})

    def _enqueue(self, op: dict) -> None:
        if len(self._outbox) == self._outbox.maxlen:
            log.warning("hubclient outbox full (%d); dropping oldest op",
                        self._outbox.maxlen)
        self._outbox.append(op)  # deque(maxlen=…) drops the oldest when full
        if self._connected and self._ws is not None:
            asyncio.create_task(self._flush())

    async def _flush(self) -> None:
        async with self._flush_lock:
            while self._outbox and self._ws is not None:
                op = self._outbox[0]
                try:
                    await self._ws.send(json.dumps(op))
                except websockets.ConnectionClosed:
                    return
                self._outbox.popleft()

    async def connect_once(self) -> bool:
        """One connection attempt. Returns True if it got `welcome` and ran
        the listen loop to completion/disconnect; False if the hub refused
        the hello (e.g. bad auth) without ever reaching the listen loop."""
        async with websockets.connect(self._url, max_size=1_048_576) as ws:
            self._ws = ws
            await ws.send(json.dumps(
                {"op": "hello", "token": self._token, "projects": self._projects()}))
            welcome = json.loads(await ws.recv())
            if welcome.get("op") != "welcome":
                log.error("hub refused: %s", welcome)
                self._connected = False
                self._ws = None
                return False
            self._connected = True
            await self._flush()
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("op") == "deliver":
                        res = self._on_deliver(msg["env"])
                        if inspect.isawaitable(res):
                            await res
            finally:
                self._connected = False
                self._ws = None
            return True

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                if await self.connect_once():
                    backoff = 1.0
            except (OSError, websockets.WebSocketException) as exc:
                log.warning("hub connect failed: %s; retry in %.0fs", exc, backoff)
            except asyncio.CancelledError:
                raise
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
