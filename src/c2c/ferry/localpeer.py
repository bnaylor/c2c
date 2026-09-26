"""The ferry's local peer identity: receive from and inject into sessions."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from typing import Callable

from c2c.ferry import registry, wire

# Max time to wait on a single read from a connected peer. A legitimate sender
# writes its auth+user lines and half-closes within milliseconds; a client that
# connects and then stalls (or never half-closes) is dropped rather than pinning
# a handler indefinitely.
READ_TIMEOUT_S = 10.0


class LocalPeer:
    def __init__(self, name: str, sessions_dir: str, sock_dir: str,
                 on_message: Callable[[dict], None]) -> None:
        self.name = name
        self.sessions_dir = sessions_dir
        self.sock_dir = sock_dir
        self._on_message = on_message
        self.pid = os.getpid()
        self.sock_path = f"{sock_dir}/{self.pid}.sock"
        self.peer_token = os.urandom(16).hex()
        self._srv: asyncio.Server | None = None

    @property
    def sock_uri(self) -> str:
        return f"uds:{self.sock_path}"

    def publish(self) -> None:
        os.makedirs(self.sock_dir, mode=0o700, exist_ok=True)
        os.makedirs(self.sessions_dir, mode=0o700, exist_ok=True)
        ps = time.strftime("%a %b %e %H:%M:%S %Y")
        h = hashlib.sha256(self.sock_path.encode()).hexdigest()  # no realpath
        um = os.umask(0o077)
        with open(os.path.join(self.sessions_dir, f"{self.pid}.{h}.key"), "w") as f:
            json.dump({"peerToken": self.peer_token, "procStart": ps}, f)
        os.umask(um)
        now = int(time.time() * 1000)
        entry = {
            "pid": self.pid,
            "sessionId": f"00000000-0000-4000-8000-{self.pid:012d}",
            "cwd": os.getcwd(), "startedAt": now, "procStart": ps,
            "version": "2.1.236", "peerProtocol": 1,
            "peerFeatures": ["notify_idle"], "kind": "interactive",
            "entrypoint": "cli", "messagingSocketPath": self.sock_path,
            "name": self.name, "nameSource": "derived", "nameSince": now,
            "status": "idle", "updatedAt": now, "statusUpdatedAt": now,
        }
        with open(os.path.join(self.sessions_dir, f"{self.pid}.json"), "w") as f:
            json.dump(entry, f)

    async def serve(self) -> None:
        if os.path.exists(self.sock_path):
            os.unlink(self.sock_path)
        self._srv = await asyncio.start_unix_server(self._handle, path=self.sock_path)
        os.chmod(self.sock_path, 0o600)
        async with self._srv:
            await self._srv.serve_forever()

    async def _handle(self, reader, writer) -> None:
        limit = wire_max()
        buf = b""
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(reader.read(65536), READ_TIMEOUT_S)
                except asyncio.TimeoutError:
                    # sender stalled / never half-closed: drop it
                    writer.close()
                    return
                if not chunk:
                    break
                buf += chunk
                if len(buf) > limit:
                    # oversized payload: stop reading and drop the connection
                    writer.close()
                    return
        except OSError:
            writer.close()
            return
        writer.close()

        lines = [ln for ln in buf.split(b"\n") if ln.strip()]
        if not lines:
            return

        try:
            auth = json.loads(lines[0])
        except ValueError:
            return
        token = auth.get("token") if isinstance(auth, dict) else None
        if (
            not isinstance(auth, dict)
            or auth.get("type") != "auth"
            or not isinstance(token, str)
            or not hmac.compare_digest(token, self.peer_token)
        ):
            return

        for line in lines[1:]:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("type") == "user":
                content = obj.get("message", {}).get("content", "")
                fields = wire.parse_wrapper(content)
                if fields is not None:
                    fields["raw_from"] = obj.get("from")
                    self._on_message(fields)

    async def inject(self, target_entry: dict, body: str,
                     hop_chain: list[str] | None, msg_id: str) -> bool:
        if not wire.protocol_ok(target_entry):
            return False
        token = registry.peer_token_for(target_entry, self.sessions_dir)
        if not token:
            return False
        msg = wire.build_user_message(self.sock_uri, self.name, body,
                                      msg_id, hop_chain)
        raw = wire.encode_frames(token, msg)
        target_sock = target_entry["messagingSocketPath"]
        try:
            reader, writer = await asyncio.open_unix_connection(target_sock)
        except OSError:
            return False
        try:
            writer.write(raw)
            await writer.drain()
            await asyncio.sleep(0.15)  # macOS buffer-flush parity
        finally:
            writer.close()
        return True

    def cleanup(self) -> None:
        if self._srv is not None:
            self._srv.close()
        h = hashlib.sha256(self.sock_path.encode()).hexdigest()
        for p in (
            self.sock_path,
            os.path.join(self.sessions_dir, f"{self.pid}.{h}.key"),
            os.path.join(self.sessions_dir, f"{self.pid}.json"),
        ):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass


def wire_max() -> int:
    from c2c.envelope import MAX_BYTES
    return MAX_BYTES
