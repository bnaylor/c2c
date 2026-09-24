import json
import asyncio
import pytest
import websockets

from c2c.hub.server import Hub
from c2c.hub.mailbox import Mailbox
from c2c.hub.auth import Auth


def env(msg_id, project="P", origin="home", created=1000):
    return {
        "v": 1, "msg_id": msg_id, "origin_host": origin, "project": project,
        "target": {"kind": "host", "host": "work", "project": project},
        "type": "note", "ttl_s": 100000, "created_at": created,
        "orig_msg_id": None, "payload": {},
    }


async def _hub(tmp_path, seed=None):
    mb = Mailbox(str(tmp_path / "m.db"))
    if seed:
        for e in seed:
            mb.put(e)
    auth = Auth({"home": "t-home", "work": "t-work"})
    hub = Hub(mb, auth, now_ms=lambda: 5000)
    server = await hub.serve("127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return hub, mb, server, port


async def test_hello_timeout_closes_connection_cleanly(tmp_path, monkeypatch):
    import c2c.hub.server as server_mod
    monkeypatch.setattr(server_mod, "HELLO_TIMEOUT_S", 0.2)
    hub, mb, server, port = await _hub(tmp_path)
    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        # never send a hello
        with pytest.raises(websockets.ConnectionClosed):
            await ws.recv()
    # server must still be alive and able to serve other connections
    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws2:
        await ws2.send(json.dumps({"op": "hello", "token": "t-work", "projects": ["P"]}))
        msg = json.loads(await ws2.recv())
        assert msg == {"op": "welcome", "host": "work"}
    server.close(); await server.wait_closed(); mb.close()


async def test_hello_bad_token_gets_error_and_close(tmp_path):
    hub, mb, server, port = await _hub(tmp_path)
    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await ws.send(json.dumps({"op": "hello", "token": "nope", "projects": []}))
        msg = json.loads(await ws.recv())
        assert msg["op"] == "error"
        with pytest.raises(websockets.ConnectionClosed):
            await ws.recv()
    server.close(); await server.wait_closed(); mb.close()


async def test_hello_non_string_token_handled_cleanly(tmp_path):
    hub, mb, server, port = await _hub(tmp_path)
    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await ws.send(json.dumps({"op": "hello", "token": ["nope"], "projects": []}))
        try:
            msg = json.loads(await ws.recv())
            assert msg["op"] == "error"
        except websockets.ConnectionClosed:
            pass  # clean close is also acceptable
    # server must still be alive and able to serve other connections
    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws2:
        await ws2.send(json.dumps({"op": "hello", "token": "t-work", "projects": ["P"]}))
        msg = json.loads(await ws2.recv())
        assert msg == {"op": "welcome", "host": "work"}
    server.close(); await server.wait_closed(); mb.close()


async def test_hello_non_list_projects_handled_cleanly(tmp_path):
    hub, mb, server, port = await _hub(tmp_path)
    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await ws.send(json.dumps({"op": "hello", "token": "t-work", "projects": 12345}))
        try:
            msg = json.loads(await ws.recv())
            assert msg["op"] == "error"
        except websockets.ConnectionClosed:
            pass  # clean close is also acceptable
    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws2:
        await ws2.send(json.dumps({"op": "hello", "token": "t-work", "projects": "PQ"}))
        try:
            msg = json.loads(await ws2.recv())
            assert msg["op"] == "error"
        except websockets.ConnectionClosed:
            pass
    # server must still be alive and able to serve other connections
    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws3:
        await ws3.send(json.dumps({"op": "hello", "token": "t-home", "projects": ["P"]}))
        msg = json.loads(await ws3.recv())
        assert msg == {"op": "welcome", "host": "home"}
    server.close(); await server.wait_closed(); mb.close()


async def test_hello_ok_gets_welcome(tmp_path):
    hub, mb, server, port = await _hub(tmp_path)
    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await ws.send(json.dumps({"op": "hello", "token": "t-work", "projects": ["P"]}))
        msg = json.loads(await ws.recv())
        assert msg == {"op": "welcome", "host": "work"}
    server.close(); await server.wait_closed(); mb.close()


async def test_backlog_drained_on_connect(tmp_path):
    hub, mb, server, port = await _hub(tmp_path, seed=[env("a"), env("b", created=2000)])
    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await ws.send(json.dumps({"op": "hello", "token": "t-work", "projects": ["P"]}))
        assert json.loads(await ws.recv())["op"] == "welcome"
        d1 = json.loads(await ws.recv())
        d2 = json.loads(await ws.recv())
        assert d1["op"] == "deliver" and d1["env"]["msg_id"] == "a"
        assert d2["env"]["msg_id"] == "b"
    server.close(); await server.wait_closed(); mb.close()


async def test_directed_backlog_delivered_even_if_host_lacks_project(tmp_path):
    # Host-directed messages must reach their target host on connect even
    # when the host hasn't announced that project (delegation guarantee).
    hub, mb, server, port = await _hub(tmp_path, seed=[env("a", project="P")])
    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await ws.send(json.dumps({"op": "hello", "token": "t-work", "projects": ["OTHER"]}))
        assert json.loads(await ws.recv())["op"] == "welcome"
        d = json.loads(await ws.recv())
        assert d["op"] == "deliver" and d["env"]["msg_id"] == "a"
    server.close(); await server.wait_closed(); mb.close()
