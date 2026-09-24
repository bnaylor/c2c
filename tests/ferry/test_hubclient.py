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


async def test_flush_is_single_flighted_no_duplicate_send(tmp_path):
    """Two enqueues back-to-back (no await between them) while connected
    used to spawn two concurrent _flush() tasks that could both read
    _outbox[0] before either popped it, double-sending one op and starving
    the other. With the flush single-flighted via a lock, each queued op
    must be sent exactly once and none dropped.

    LIMITATION: this is not a deterministic reproduction of the race.
    Verified empirically (by temporarily reverting the lock) that this
    assertion still passes on the pre-fix code on this machine: over a
    loopback connection, `ws.send()` for a small payload typically
    completes without truly suspending back to the event loop, so task1
    usually drains the whole outbox before task2 (scheduled via
    create_task) gets a turn, and the interleaving the Important fix
    guards against does not reliably open in this timing. A guaranteed
    reproduction would need to force a real suspension between the read of
    _outbox[0] and the pop (e.g. a fake transport/monkeypatched `ws.send`
    that awaits an event before returning), which was judged not worth
    the complexity for this bundled fix; kept as a correctness-preserving
    regression check per the reviewer's documented fallback instead."""
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

    # No `await` between these two calls: both _enqueue() calls run while
    # already connected, so (pre-fix) each spawns its own _flush() task
    # that can race the other.
    home.post(env("x", {"kind": "host", "host": "work", "project": "P"}))
    home.post(env("y", {"kind": "host", "host": "work", "project": "P"}))
    await asyncio.sleep(0.3)

    ids = [e["msg_id"] for e in got]
    assert sorted(ids) == ["x", "y"], (
        "expected exactly one delivery each of x and y with no duplicate "
        f"or dropped op; got {ids!r}"
    )
    work_task.cancel(); home_task.cancel()
    server.close(); await server.wait_closed(); mb.close()


async def test_auth_refusal_backoff_grows(monkeypatch):
    """Backoff must grow 1,2,4,8 across repeated auth-refused attempts instead
    of resetting to 1.0 each time. Driven deterministically: connect_once is
    stubbed to report auth-refusal (False) with no network, and hubclient's
    asyncio.sleep is replaced by a recorder that yields instantly, so the
    recorded delays are exactly the backoff sequence with no timing races."""
    delays = []
    real_asyncio = asyncio

    class _FakeAsyncio:
        def __getattr__(self, name):
            return getattr(real_asyncio, name)

        async def sleep(self, d):
            delays.append(d)
            await real_asyncio.sleep(0)

    monkeypatch.setattr("c2c.ferry.hubclient.asyncio", _FakeAsyncio())

    client = HubClient("ws://127.0.0.1:1", "tok",
                       projects_provider=lambda: [], on_deliver=lambda e: None)

    async def _refuse():
        return False  # simulate an auth-refused connect_once, no network

    monkeypatch.setattr(client, "connect_once", _refuse)

    task = asyncio.create_task(client.run())
    for _ in range(1000):
        if len(delays) >= 4:
            break
        await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert delays[:4] == [1.0, 2.0, 4.0, 8.0], f"got {delays[:4]!r}"


async def test_auth_refusal_clears_stale_state(tmp_path):
    """On the non-welcome (auth-refused) path, connect_once returns False and
    leaves no dangling ws/connected state."""
    mb, server, port = await _hub(tmp_path)
    client = HubClient(f"ws://127.0.0.1:{port}", "wrong-token",
                       projects_provider=lambda: ["P"], on_deliver=lambda e: None)
    ok = await client.connect_once()
    assert ok is False
    assert client._ws is None
    assert client._connected is False
    server.close(); await server.wait_closed(); mb.close()
