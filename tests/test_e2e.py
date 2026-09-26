"""End-to-end integration tests: real hub + two ferries (home, work) + fake
sessions. The grown-up tap.py spike as automated tests.

- test_home_to_work_and_back: post from home, assert it lands in the work
  session, reply, assert the reply comes back to home.
- test_reply_returns_to_the_asking_session: the same round trip with two
  sessions on the home side, asserting the answer reaches the one that asked
  rather than the bg session pick_target prefers.

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


def make_bg_session(sessions_dir, sock_dir, pid, name, cwd, kind="bg",
                   status_updated_at=1):
    sock = f"{sock_dir}/{pid}.sock"
    h = hashlib.sha256(sock.encode()).hexdigest()
    (sessions_dir / f"{pid}.{h}.key").write_text(json.dumps({"peerToken": "st"}))
    (sessions_dir / f"{pid}.json").write_text(json.dumps({
        "pid": pid, "name": name, "cwd": cwd, "kind": kind,
        "sessionId": f"sess-{pid}",
        "peerProtocol": 1, "version": "2.1.236",
        "messagingSocketPath": sock, "statusUpdatedAt": status_updated_at}))
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

        # Ferry.on_deliver now records the pending-reply correlation BEFORE
        # calling inject(), so a fast reply is correctly classified even
        # with no pause here. This tiny sleep is just local-socket
        # scheduling slack, not a workaround for the correlation race.
        await asyncio.sleep(0.05)

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


async def test_reply_returns_to_the_asking_session(tmp_path):
    """The whole path for the bug: an interactive session on home asks, a bg
    session on work answers, and the answer must come back to the interactive
    session -- even though home also has a bg session on the same project that
    pick_target would otherwise prefer."""
    work_socks_path = tempfile.mkdtemp(prefix="c2c-", dir="/tmp")
    home_socks_path = tempfile.mkdtemp(prefix="c2c-", dir="/tmp")
    try:
        mb = Mailbox(str(tmp_path / "hub.db"))
        hub = Hub(mb, Auth({"home": "t-home", "work": "t-work"}))
        server = await hub.serve("127.0.0.1", 0)
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        PROJECT = "github.com/sackheads/iris"

        def build(host, peer, token, sessions_dir, socks):
            cfg = FerryConfig(host_id=host, peer_host=peer, hub_url=url,
                              token=token, sessions_dir=str(sessions_dir),
                              sock_dir=socks)
            peer_obj = LocalPeer(peer, str(sessions_dir), socks, on_message=None)
            hc = HubClient(url, token, lambda: [PROJECT], None)
            f = Ferry(cfg, peer_obj, hc, Dedup(600000, lambda: 0),
                      projectfn=lambda cwd: PROJECT, now_ms=lambda: 10)
            peer_obj._on_message = f.on_local_message
            hc._on_deliver = f.on_deliver
            peer_obj.publish()
            return f, peer_obj, hc

        # work: one bg session that will answer
        work_dir = tmp_path / "work_sessions"; work_dir.mkdir()
        wbg_srv, wbg_sock = make_bg_session(work_dir, work_socks_path, 710,
                                            "iris-bg", "/wrepo")
        _wf, work_peer, work_hub = build("work", "home", "t-work",
                                         work_dir, work_socks_path)

        # home: the interactive session that asks, plus a bg session on the
        # same project that pick_target would prefer if the pin didn't win.
        home_dir = tmp_path / "home_sessions"; home_dir.mkdir()
        ask_srv, ask_sock = make_bg_session(home_dir, home_socks_path, 720,
                                            "iris-ac", "/hrepo",
                                            kind="interactive")
        hbg_srv, _hbg_sock = make_bg_session(home_dir, home_socks_path, 721,
                                             "iris-bg", "/hrepo",
                                             status_updated_at=999)
        _hf, home_peer, home_hub = build("home", "work", "t-home",
                                         home_dir, home_socks_path)

        tasks = [asyncio.create_task(t) for t in (
            work_peer.serve(), work_hub.run(), home_peer.serve(), home_hub.run())]
        await asyncio.sleep(0.4)  # connect + announce
        loop = asyncio.get_running_loop()

        # The interactive session on home sends through the real wire, so the
        # ferry pins it as the origin exactly as it would in production.
        ask = wire.build_user_message(ask_sock, "iris-ac", "review PR 42", "ask-mid")
        _r, w = await asyncio.open_unix_connection(home_peer.sock_path)
        w.write(wire.encode_frames(home_peer.peer_token, ask))
        await w.drain(); w.close()

        injected = await asyncio.wait_for(_read_injected(loop, wbg_srv), timeout=3)
        assert injected["body"] == "review PR 42"

        reply = wire.build_user_message(wbg_sock, "iris-bg", "done, LGTM",
                                        "reply-mid", hop_chain=injected["hop_chain"])
        _r2, w2 = await asyncio.open_unix_connection(work_peer.sock_path)
        w2.write(wire.encode_frames(work_peer.peer_token, reply))
        await w2.drain(); w2.close()

        # Whichever home session the ferry connects to first, wins.
        accepts = {"asker": asyncio.create_task(loop.sock_accept(ask_srv)),
                   "home-bg": asyncio.create_task(loop.sock_accept(hbg_srv))}
        done, _ = await asyncio.wait(accepts.values(), timeout=3,
                                    return_when=asyncio.FIRST_COMPLETED)
        landed = [label for label, t in accepts.items() if t in done]
        assert landed == ["asker"], f"reply landed in {landed}"

        conn = accepts["asker"].result()[0]
        data = b""
        while data.count(b"\n") < 2:
            c = await loop.sock_recv(conn, 65536)
            if not c: break
            data += c
        _auth, user = [json.loads(x) for x in data.decode().strip().split("\n")]
        assert wire.parse_wrapper(user["message"]["content"])["body"] == "done, LGTM"
        conn.close()
        for label, t in accepts.items():
            if t not in done:
                t.cancel()

        for t in tasks:
            t.cancel()
        work_peer.cleanup(); home_peer.cleanup()
        for s in (wbg_srv, ask_srv, hbg_srv):
            s.close()
        server.close(); await server.wait_closed(); mb.close()
    finally:
        shutil.rmtree(work_socks_path, ignore_errors=True)
        shutil.rmtree(home_socks_path, ignore_errors=True)


async def test_offline_peer_is_reported_back_into_the_asking_session(tmp_path):
    """No work ferry is connected, so the hub stores the note and says nobody
    can take it. That report has to come back as a message in the transcript of
    the session that sent it -- the whole point of the hub telling us."""
    home_socks_path = tempfile.mkdtemp(prefix="c2c-", dir="/tmp")
    try:
        mb = Mailbox(str(tmp_path / "hub.db"))
        hub = Hub(mb, Auth({"home": "t-home", "work": "t-work"}))
        server = await hub.serve("127.0.0.1", 0)
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        PROJECT = "github.com/sackheads/iris"

        home_dir = tmp_path / "home_sessions"; home_dir.mkdir()
        ask_srv, ask_sock = make_bg_session(home_dir, home_socks_path, 730,
                                            "iris-ac", "/hrepo",
                                            kind="interactive")
        cfg = FerryConfig(host_id="home", peer_host="work", hub_url=url,
                          token="t-home", sessions_dir=str(home_dir),
                          sock_dir=home_socks_path)
        home_peer = LocalPeer("work", str(home_dir), home_socks_path, on_message=None)
        home_hub = HubClient(url, "t-home", lambda: [PROJECT], None)
        ferry = Ferry(cfg, home_peer, home_hub, Dedup(600000, lambda: 0),
                      projectfn=lambda cwd: PROJECT, now_ms=lambda: 10)
        home_peer._on_message = ferry.on_local_message
        home_hub._on_deliver = ferry.on_deliver
        home_hub._on_status = ferry.on_hub_status
        home_peer.publish()
        tasks = [asyncio.create_task(t) for t in (home_peer.serve(), home_hub.run())]
        await asyncio.sleep(0.4)

        loop = asyncio.get_running_loop()
        reader = asyncio.create_task(_read_injected(loop, ask_srv))
        ask = wire.build_user_message(ask_sock, "iris-ac", "review PR 42", "ask-mid")
        _r, w = await asyncio.open_unix_connection(home_peer.sock_path)
        w.write(wire.encode_frames(home_peer.peer_token, ask))
        await w.drain(); w.close()

        got = await asyncio.wait_for(reader, timeout=3)
        assert "work" in got["body"] and "held" in got["body"].lower()
        # and the note itself is still in the mailbox waiting for work
        assert [e["payload"]["body"] for e in mb.pending_for("work", {PROJECT}, 20)] \
            == ["review PR 42"]

        for t in tasks:
            t.cancel()
        home_peer.cleanup(); ask_srv.close()
        server.close(); await server.wait_closed(); mb.close()
    finally:
        shutil.rmtree(home_socks_path, ignore_errors=True)
