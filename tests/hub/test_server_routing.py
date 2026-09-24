import json
import asyncio
import pytest
import websockets

from c2c.hub.server import Hub
from c2c.hub.mailbox import Mailbox
from c2c.hub.auth import Auth


def env(msg_id, target, project="P", origin="home", created=1000):
    return {
        "v": 1, "msg_id": msg_id, "origin_host": origin, "project": project,
        "target": target, "type": "note", "ttl_s": 100000,
        "created_at": created, "orig_msg_id": None, "payload": {},
    }


def host_t(host, project="P"):
    return {"kind": "host", "host": host, "project": project}


def proj_t(project="P"):
    return {"kind": "project", "project": project}


async def _hub(tmp_path):
    mb = Mailbox(str(tmp_path / "m.db"))
    auth = Auth({"home": "t-home", "work": "t-work", "lap": "t-lap"})
    hub = Hub(mb, auth, now_ms=lambda: 5000)
    server = await hub.serve("127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return hub, mb, server, port


async def _connect(port, token, projects):
    ws = await websockets.connect(f"ws://127.0.0.1:{port}")
    await ws.send(json.dumps({"op": "hello", "token": token, "projects": projects}))
    assert json.loads(await ws.recv())["op"] == "welcome"
    return ws


async def test_directed_post_pushed_live(tmp_path):
    hub, mb, server, port = await _hub(tmp_path)
    work = await _connect(port, "t-work", ["P"])
    home = await _connect(port, "t-home", ["P"])
    await home.send(json.dumps({"op": "post", "env": env("a", host_t("work"))}))
    d = json.loads(await work.recv())
    assert d["op"] == "deliver" and d["env"]["msg_id"] == "a"
    await work.close(); await home.close()
    server.close(); await server.wait_closed(); mb.close()


async def test_fanout_skips_origin_and_nonsubscribers(tmp_path):
    hub, mb, server, port = await _hub(tmp_path)
    home = await _connect(port, "t-home", ["P"])
    work = await _connect(port, "t-work", ["P"])
    lap = await _connect(port, "t-lap", ["OTHER"])
    await home.send(json.dumps({"op": "post", "env": env("a", proj_t("P"), origin="home")}))
    d = json.loads(await work.recv())
    assert d["env"]["msg_id"] == "a"
    # origin (home) and non-subscriber (lap) get nothing
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(home.recv(), timeout=0.3)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(lap.recv(), timeout=0.3)
    await home.close(); await work.close(); await lap.close()
    server.close(); await server.wait_closed(); mb.close()


async def test_ack_prevents_redelivery_on_reconnect(tmp_path):
    hub, mb, server, port = await _hub(tmp_path)
    home = await _connect(port, "t-home", ["P"])
    work = await _connect(port, "t-work", ["P"])
    await home.send(json.dumps({"op": "post", "env": env("a", host_t("work"))}))
    d = json.loads(await work.recv())
    await work.send(json.dumps({"op": "ack", "msg_id": d["env"]["msg_id"]}))
    await asyncio.sleep(0.1)  # let the ack land
    await work.close()
    work2 = await _connect(port, "t-work", ["P"])  # reconnect: no redelivery
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(work2.recv(), timeout=0.3)
    await home.close(); await work2.close()
    server.close(); await server.wait_closed(); mb.close()


async def test_announce_drains_newly_eligible(tmp_path):
    hub, mb, server, port = await _hub(tmp_path)
    home = await _connect(port, "t-home", ["P"])
    work = await _connect(port, "t-work", ["OTHER"])          # not subscribed to P yet
    await home.send(json.dumps({"op": "post", "env": env("a", proj_t("P"))}))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(work.recv(), timeout=0.3)      # nothing yet
    await work.send(json.dumps({"op": "announce", "projects": ["OTHER", "P"]}))
    d = json.loads(await work.recv())
    assert d["env"]["msg_id"] == "a"
    await home.close(); await work.close()
    server.close(); await server.wait_closed(); mb.close()


async def test_bad_post_errors_but_keeps_connection(tmp_path):
    hub, mb, server, port = await _hub(tmp_path)
    home = await _connect(port, "t-home", ["P"])
    await home.send(json.dumps({"op": "post", "env": {"bad": "envelope"}}))
    m = json.loads(await home.recv())
    assert m["op"] == "error"
    # connection still usable
    await home.send(json.dumps({"op": "post", "env": env("a", host_t("home"))}))
    await asyncio.sleep(0.1)
    await home.close()
    server.close(); await server.wait_closed(); mb.close()
