import asyncio
import hashlib
import json
import os
import socket
import pytest
from c2c.ferry.localpeer import LocalPeer
from c2c.ferry import wire


class FakeSession:
    """A stand-in Claude session: registry entry + key + a listening socket."""
    def __init__(self, sessions_dir, sock_dir, pid, name, cwd, peer_protocol=1):
        self.dir = sessions_dir
        self.pid = pid
        self.sock_path = f"{sock_dir}/{pid}.sock"
        self.token = "sess-token"
        h = hashlib.sha256(self.sock_path.encode()).hexdigest()
        (sessions_dir / f"{pid}.{h}.key").write_text(json.dumps({"peerToken": self.token}))
        self.entry = {
            "pid": pid, "name": name, "cwd": cwd, "kind": "bg",
            "peerProtocol": peer_protocol, "version": "2.1.236",
            "messagingSocketPath": self.sock_path, "statusUpdatedAt": 1,
        }
        (sessions_dir / f"{pid}.json").write_text(json.dumps(self.entry))
        if os.path.exists(self.sock_path):
            os.unlink(self.sock_path)
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.sock_path)
        self.srv.listen(4)
        self.srv.setblocking(False)

    async def recv_one(self):
        loop = asyncio.get_running_loop()
        conn, _ = await loop.sock_accept(self.srv)
        buf = b""
        while b"\n" not in buf or buf.count(b"\n") < 2:
            chunk = await loop.sock_recv(conn, 65536)
            if not chunk:
                break
            buf += chunk
        conn.close()
        lines = buf.decode().strip().split("\n")
        return json.loads(lines[0]), json.loads(lines[1])  # auth, user

    def close(self):
        self.srv.close()


async def test_inject_delivers_authed_user_frame(tmp_path):
    sock_dir = tmp_path / "socks"; sock_dir.mkdir()
    fs = FakeSession(tmp_path, str(sock_dir), 200, "worker", "/repo")
    lp = LocalPeer("work", str(tmp_path), str(sock_dir), on_message=lambda f: None)
    lp.publish()
    recv = asyncio.create_task(fs.recv_one())
    ok = await lp.inject(fs.entry, "do the thing", ["aa" * 12], "mid-1")
    auth, user = await recv
    assert ok is True
    assert auth == {"type": "auth", "token": "sess-token"}
    parsed = wire.parse_wrapper(user["message"]["content"])
    assert parsed["body"] == "do the thing"
    assert parsed["hop_chain"] == ["aa" * 12]
    assert parsed["from"] == lp.sock_uri          # reply routes back to us
    assert parsed["from_name"] == "work"
    lp.cleanup(); fs.close()


async def test_inject_refuses_on_protocol_mismatch(tmp_path):
    sock_dir = tmp_path / "socks"; sock_dir.mkdir()
    fs = FakeSession(tmp_path, str(sock_dir), 201, "w", "/repo", peer_protocol=2)
    lp = LocalPeer("work", str(tmp_path), str(sock_dir), on_message=lambda f: None)
    lp.publish()
    ok = await lp.inject(fs.entry, "x", None, "mid-2")
    assert ok is False
    lp.cleanup(); fs.close()


async def test_serve_invokes_on_message(tmp_path):
    sock_dir = tmp_path / "socks"; sock_dir.mkdir()
    received = []
    lp = LocalPeer("work", str(tmp_path), str(sock_dir),
                   on_message=lambda f: received.append(f))
    lp.publish()
    server = asyncio.create_task(lp.serve())
    await asyncio.sleep(0.05)
    # a local "session" sends a message to our peer
    msg = wire.build_user_message("uds:/tmp/cc-socks/900.sock", "iris-x",
                                  "please review", "mid-3", hop_chain=["bb" * 12])
    raw = wire.encode_frames(lp.peer_token, msg)
    reader, writer = await asyncio.open_unix_connection(lp.sock_path)
    writer.write(raw); await writer.drain()
    writer.close()
    await asyncio.sleep(0.1)
    lp.cleanup(); server.cancel()
    assert len(received) == 1
    assert received[0]["body"] == "please review"
    assert received[0]["hop_chain"] == ["bb" * 12]
    assert received[0]["raw_from"] == "uds:/tmp/cc-socks/900.sock"
