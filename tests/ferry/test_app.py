import asyncio
import json
import hashlib
import os
import socket
import pytest
from c2c.ferry.app import Ferry
from c2c.ferry.config import FerryConfig
from c2c.ferry.localpeer import LocalPeer
from c2c.ferry.dedup import Dedup
from c2c.ferry import wire


class Recorder:  # stand-in hub client
    def __init__(self):
        self.posts = []; self.acks = []
    def post(self, env): self.posts.append(env)
    def ack(self, mid): self.acks.append(mid)


def make_session(sessions_dir, sock_dir, pid, name, cwd):
    sock = f"{sock_dir}/{pid}.sock"
    tok = "sess-tok"
    h = hashlib.sha256(sock.encode()).hexdigest()
    (sessions_dir / f"{pid}.{h}.key").write_text(json.dumps({"peerToken": tok}))
    entry = {"pid": pid, "name": name, "cwd": cwd, "kind": "bg",
             "peerProtocol": 1, "version": "2.1.236",
             "messagingSocketPath": sock, "statusUpdatedAt": 1}
    (sessions_dir / f"{pid}.json").write_text(json.dumps(entry))
    return entry, sock, tok


def cfg(sessions_dir, sock_dir):
    return FerryConfig(host_id="work", peer_host="home",
                       hub_url="ws://x", token="t",
                       sessions_dir=str(sessions_dir), sock_dir=str(sock_dir))


async def test_on_deliver_injects_and_acks(tmp_path, short_sock_dir):
    entry, sock, tok = make_session(tmp_path, short_sock_dir, 300, "iris-bg", "/repo")
    # a listening fake session socket
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock); srv.listen(2); srv.setblocking(False)

    lp = LocalPeer("home", str(tmp_path), short_sock_dir, on_message=lambda f: None)
    lp.publish()
    rec = Recorder()
    ferry = Ferry(cfg(tmp_path, short_sock_dir), lp, rec,
                  Dedup(600000, lambda: 0),
                  projectfn=lambda cwd: "PROJ", now_ms=lambda: 1000)

    env = {"v": 1, "msg_id": "m1", "origin_host": "home", "project": "PROJ",
           "target": {"kind": "host", "host": "work", "project": "PROJ"},
           "type": "note", "ttl_s": 100000, "created_at": 1,
           "orig_msg_id": None, "payload": {"body": "run tests", "hop_chain": None}}

    loop = asyncio.get_running_loop()
    accept = asyncio.create_task(loop.sock_accept(srv))
    await ferry.on_deliver(env)
    conn, _ = await accept
    data = b""
    while data.count(b"\n") < 2:
        c = await loop.sock_recv(conn, 65536)
        if not c: break
        data += c
    _auth, user = [json.loads(x) for x in data.decode().strip().split("\n")]
    assert wire.parse_wrapper(user["message"]["content"])["body"] == "run tests"
    assert rec.acks == ["m1"]
    lp.cleanup(); srv.close(); conn.close()


async def test_on_deliver_dedup_acks_without_inject(tmp_path, short_sock_dir):
    lp = LocalPeer("home", str(tmp_path), short_sock_dir, on_message=lambda f: None)
    lp.publish()
    rec = Recorder()
    d = Dedup(600000, lambda: 0); d.seen("m1")  # already seen
    ferry = Ferry(cfg(tmp_path, short_sock_dir), lp, rec, d,
                  projectfn=lambda cwd: "PROJ", now_ms=lambda: 1)
    env = {"v": 1, "msg_id": "m1", "origin_host": "home", "project": "PROJ",
           "target": {"kind": "host", "host": "work", "project": "PROJ"},
           "type": "note", "ttl_s": 1, "created_at": 1, "orig_msg_id": None,
           "payload": {"body": "x", "hop_chain": None}}
    await ferry.on_deliver(env)
    assert rec.acks == ["m1"] and rec.posts == []
    lp.cleanup()


def test_on_local_message_posts_note(tmp_path, short_sock_dir):
    entry, sock, tok = make_session(tmp_path, short_sock_dir, 400, "iris-x", "/repo")
    lp = LocalPeer("home", str(tmp_path), short_sock_dir, on_message=lambda f: None)
    lp.publish()
    rec = Recorder()
    ferry = Ferry(cfg(tmp_path, short_sock_dir), lp, rec, Dedup(1, lambda: 0),
                  projectfn=lambda cwd: "PROJ", now_ms=lambda: 1234)
    ferry.on_local_message({"raw_from": f"uds:{sock}", "from_name": "iris-x",
                            "hop_chain": None, "body": "review pr 42"})
    assert len(rec.posts) == 1
    e = rec.posts[0]
    assert e["type"] == "note" and e["project"] == "PROJ"
    assert e["origin_host"] == "work"
    assert e["target"] == {"kind": "host", "host": "home", "project": "PROJ"}
    assert e["payload"]["body"] == "review pr 42"
    lp.cleanup()


async def test_reply_correlation(tmp_path, short_sock_dir):
    entry, sock, tok = make_session(tmp_path, short_sock_dir, 500, "iris-bg", "/repo")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock); srv.listen(2); srv.setblocking(False)
    lp = LocalPeer("home", str(tmp_path), short_sock_dir, on_message=lambda f: None)
    lp.publish()
    rec = Recorder()
    ferry = Ferry(cfg(tmp_path, short_sock_dir), lp, rec, Dedup(600000, lambda: 0),
                  projectfn=lambda cwd: "PROJ", now_ms=lambda: 1)
    env = {"v": 1, "msg_id": "orig-9", "origin_host": "home", "project": "PROJ",
           "target": {"kind": "host", "host": "work", "project": "PROJ"},
           "type": "note", "ttl_s": 100000, "created_at": 1, "orig_msg_id": None,
           "payload": {"body": "please review", "hop_chain": None}}
    loop = asyncio.get_running_loop()
    accept = asyncio.create_task(loop.sock_accept(srv))
    await ferry.on_deliver(env)
    conn, _ = await accept; conn.close()
    # now the session replies to our peer
    ferry.on_local_message({"raw_from": f"uds:{sock}", "from_name": "iris-bg",
                            "hop_chain": None, "body": "done"})
    e = rec.posts[0]
    assert e["type"] == "reply" and e["orig_msg_id"] == "orig-9"
    lp.cleanup(); srv.close()
