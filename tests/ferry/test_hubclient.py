import asyncio
import pytest
from c2c.hub.server import Hub
from c2c.hub.mailbox import Mailbox
from c2c.hub.auth import Auth
from c2c.ferry.hubclient import HubClient


def env(msg_id, target, project="P", origin="home"):
    return {
        "v": 1, "msg_id": msg_id, "origin_host": origin, "project": project,
        "target": target, "type": "note", "ttl_s": 100000,
        "created_at": 1000, "orig_msg_id": None, "payload": {"body": "hi"},
    }


async def _hub(tmp_path):
    mb = Mailbox(str(tmp_path / "m.db"))
    auth = Auth({"home": "t-home", "work": "t-work"})
    hub = Hub(mb, auth, now_ms=lambda: 5000)
    server = await hub.serve("127.0.0.1", 0)
    return mb, server, server.sockets[0].getsockname()[1]


async def test_post_then_peer_receives(tmp_path):
    mb, server, port = await _hub(tmp_path)
    got = []
    work = HubClient(f"ws://127.0.0.1:{port}", "t-work",
                     projects_provider=lambda: ["P"], on_deliver=got.append)
    work_task = asyncio.create_task(work.run())
    await asyncio.sleep(0.2)  # let work connect + announce P

    home = HubClient(f"ws://127.0.0.1:{port}", "t-home",
                     projects_provider=lambda: ["P"], on_deliver=lambda e: None)
    home_task = asyncio.create_task(home.run())
    await asyncio.sleep(0.2)
    home.post(env("a", {"kind": "host", "host": "work", "project": "P"}))
    await asyncio.sleep(0.3)

    assert [e["msg_id"] for e in got] == ["a"]
    work_task.cancel(); home_task.cancel()
    server.close(); await server.wait_closed(); mb.close()


async def test_backlog_delivered_on_connect(tmp_path):
    mb, server, port = await _hub(tmp_path)
    mb.put(env("b", {"kind": "host", "host": "work", "project": "P"}))
    got = []
    work = HubClient(f"ws://127.0.0.1:{port}", "t-work",
                     projects_provider=lambda: ["P"], on_deliver=got.append)
    task = asyncio.create_task(work.run())
    await asyncio.sleep(0.3)
    assert [e["msg_id"] for e in got] == ["b"]
    task.cancel(); server.close(); await server.wait_closed(); mb.close()


async def test_ack_stops_redelivery(tmp_path):
    mb, server, port = await _hub(tmp_path)
    mb.put(env("c", {"kind": "host", "host": "work", "project": "P"}))
    got = []

    async def on_deliver(e):
        got.append(e)
        work.ack(e["msg_id"])

    work = HubClient(f"ws://127.0.0.1:{port}", "t-work",
                     projects_provider=lambda: ["P"], on_deliver=on_deliver)
    task = asyncio.create_task(work.run())
    await asyncio.sleep(0.3)
    task.cancel()
    await asyncio.sleep(0.1)
    # reconnect: should NOT get "c" again
    got2 = []
    work2 = HubClient(f"ws://127.0.0.1:{port}", "t-work",
                      projects_provider=lambda: ["P"], on_deliver=got2.append)
    task2 = asyncio.create_task(work2.run())
    await asyncio.sleep(0.3)
    assert [e["msg_id"] for e in got] == ["c"]
    assert got2 == []
    task2.cancel(); server.close(); await server.wait_closed(); mb.close()
