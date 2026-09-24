"""End-to-end integration test: real hub + two ferries (home, work) + a fake
bg session on the work side. This is the grown-up tap.py spike as an
automated test: post from home, assert it lands in the work session, reply,
assert the reply comes back to home.

NOTE on socket paths: macOS caps AF_UNIX sun_path at ~104 bytes. pytest's
default tmp_path lives under /var/folders/... which is too long for that
limit once we append sock_dir/<pid>.sock. So the two socket directories
(work side, home side) are created under a short /tmp path via
tempfile.mkdtemp, and cleaned up in a finally block. Everything else
(sessions dirs, hub sqlite db, key files) has no such length limit and can
stay under pytest's tmp_path.
"""
import asyncio
import hashlib
import json
import shutil
import socket
import tempfile

from c2c.hub.server import Hub
from c2c.hub.mailbox import Mailbox
from c2c.hub.auth import Auth
from c2c.ferry.app import Ferry
from c2c.ferry.config import FerryConfig
from c2c.ferry.dedup import Dedup
from c2c.ferry.hubclient import HubClient
from c2c.ferry.localpeer import LocalPeer
from c2c.ferry import wire


def make_bg_session(sessions_dir, sock_dir, pid, name, cwd):
    sock = f"{sock_dir}/{pid}.sock"
    h = hashlib.sha256(sock.encode()).hexdigest()
    (sessions_dir / f"{pid}.{h}.key").write_text(json.dumps({"peerToken": "st"}))
    (sessions_dir / f"{pid}.json").write_text(json.dumps({
        "pid": pid, "name": name, "cwd": cwd, "kind": "bg",
        "peerProtocol": 1, "version": "2.1.236",
        "messagingSocketPath": sock, "statusUpdatedAt": 1}))
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock); srv.listen(4); srv.setblocking(False)
    return srv, sock


async def _read_injected(loop, srv):
    conn, _ = await loop.sock_accept(srv)
    data = b""
    while data.count(b"\n") < 2:
        c = await loop.sock_recv(conn, 65536)
        if not c: break
        data += c
    conn.close()
    _auth, user = [json.loads(x) for x in data.decode().strip().split("\n")]
    return wire.parse_wrapper(user["message"]["content"])


async def test_home_to_work_and_back(tmp_path):
    # Short /tmp-rooted socket dirs to stay under macOS's ~104-byte
    # AF_UNIX sun_path limit (pytest's tmp_path is too long for that).
    work_socks_path = tempfile.mkdtemp(prefix="c2c-", dir="/tmp")
    home_socks_path = tempfile.mkdtemp(prefix="c2c-", dir="/tmp")
    try:
        # --- hub ---
        mb = Mailbox(str(tmp_path / "hub.db"))
        auth = Auth({"home": "t-home", "work": "t-work"})
        hub = Hub(mb, auth)
        server = await hub.serve("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}"
        PROJECT = "github.com/sackheads/iris"

        # --- work host: sessions dir, a bg session, a ferry ---
        work_dir = tmp_path / "work_sessions"; work_dir.mkdir()
        bg_srv, bg_sock = make_bg_session(work_dir, work_socks_path, 700, "iris-bg", "/wrepo")
        work_cfg = FerryConfig(host_id="work", peer_host="home", hub_url=url,
                               token="t-work", sessions_dir=str(work_dir),
                               sock_dir=work_socks_path)
        work_peer = LocalPeer("home", str(work_dir), work_socks_path, on_message=None)
        work_hub = HubClient(url, "t-work", lambda: [PROJECT], None)
        work_ferry = Ferry(work_cfg, work_peer, work_hub, Dedup(600000, lambda: 0),
                           projectfn=lambda cwd: PROJECT, now_ms=lambda: 10)
        work_peer._on_message = work_ferry.on_local_message
        work_hub._on_deliver = work_ferry.on_deliver
        work_peer.publish()
        work_serve = asyncio.create_task(work_peer.serve())
        work_link = asyncio.create_task(work_hub.run())

        # --- home host: a ferry with no sessions (just posts) ---
        home_dir = tmp_path / "home_sessions"; home_dir.mkdir()
        home_cfg = FerryConfig(host_id="home", peer_host="work", hub_url=url,
                               token="t-home", sessions_dir=str(home_dir),
                               sock_dir=home_socks_path)
        home_peer = LocalPeer("work", str(home_dir), home_socks_path, on_message=None)
        got_home = []
        home_hub = HubClient(url, "t-home", lambda: [PROJECT], None)
        home_ferry = Ferry(home_cfg, home_peer, home_hub, Dedup(600000, lambda: 0),
                           projectfn=lambda cwd: PROJECT, now_ms=lambda: 20)
        home_peer._on_message = home_ferry.on_local_message
        # capture what home would inject (home has no local session, so record instead)
        async def home_deliver(env):
            got_home.append(env)
            home_hub.ack(env["msg_id"])
        home_hub._on_deliver = home_deliver
        home_peer.publish()
        home_serve = asyncio.create_task(home_peer.serve())
        home_link = asyncio.create_task(home_hub.run())

        await asyncio.sleep(0.4)  # both connect + announce

        loop = asyncio.get_running_loop()
        # home posts a directed note to work
        home_hub.post(home_ferry.build_envelope(
            PROJECT, "review PR 42", None, "note", None))

        injected = await asyncio.wait_for(_read_injected(loop, bg_srv), timeout=3)
        assert injected["body"] == "review PR 42"
        assert injected["from"] == work_peer.sock_uri  # reply routes to work ferry

        # LocalPeer.inject() writes to the bg socket, then sleeps 0.15s
        # ("macOS buffer-flush parity") before work_ferry.on_deliver records
        # the pending-reply correlation and acks. The bg socket already has
        # the bytes at this point (that's how _read_injected returned), so
        # without this pause the reply below can race ahead of that
        # bookkeeping and land as a plain "note" instead of a "reply".
        await asyncio.sleep(0.3)

        # the work bg session replies to the work ferry's peer socket
        reply = wire.build_user_message(bg_sock, "iris-bg", "done, LGTM",
                                        "reply-mid",
                                        hop_chain=injected["hop_chain"])
        r, w = await asyncio.open_unix_connection(work_peer.sock_path)
        w.write(wire.encode_frames(work_peer.peer_token, reply)); await w.drain(); w.close()

        await asyncio.wait_for(_wait_for_reply(got_home), timeout=3)
        assert any(e["payload"]["body"] == "done, LGTM" and e["type"] == "reply"
                   for e in got_home)

        for t in (work_serve, work_link, home_serve, home_link):
            t.cancel()
        work_peer.cleanup(); home_peer.cleanup()
        bg_srv.close(); server.close(); await server.wait_closed(); mb.close()
    finally:
        shutil.rmtree(work_socks_path, ignore_errors=True)
        shutil.rmtree(home_socks_path, ignore_errors=True)


async def _wait_for_reply(got_home):
    while not any(e["payload"]["body"] == "done, LGTM" and e["type"] == "reply"
                  for e in got_home):
        await asyncio.sleep(0.05)
