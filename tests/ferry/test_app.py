import asyncio
import json
import hashlib
import os
import pathlib
import socket
import pytest
from c2c.ferry.app import Ferry, NOTIFY_COOLDOWN_MS
from c2c.ferry.config import FerryConfig
from c2c.ferry.localpeer import LocalPeer
from c2c.ferry.dedup import Dedup
from c2c.ferry import wire


class Recorder:  # stand-in hub client
    def __init__(self):
        self.posts = []; self.acks = []
    def post(self, env): self.posts.append(env)
    def ack(self, mid): self.acks.append(mid)


def make_session(sessions_dir, sock_dir, pid, name, cwd, kind="bg",
                 session_id=None, status_updated_at=1):
    sock = f"{sock_dir}/{pid}.sock"
    tok = "sess-tok"
    h = hashlib.sha256(sock.encode()).hexdigest()
    (sessions_dir / f"{pid}.{h}.key").write_text(json.dumps({"peerToken": tok}))
    entry = {"pid": pid, "name": name, "cwd": cwd, "kind": kind,
             "sessionId": session_id or f"sess-{pid}",
             "peerProtocol": 1, "version": "2.1.236",
             "messagingSocketPath": sock, "statusUpdatedAt": status_updated_at}
    (sessions_dir / f"{pid}.json").write_text(json.dumps(entry))
    return entry, sock, tok


def listen(sock):
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock); srv.listen(2); srv.setblocking(False)
    return srv


async def read_injected(srv, timeout=1.0):
    """Accept one connection on `srv` and return the parsed wrapper fields."""
    loop = asyncio.get_running_loop()
    conn, _ = await asyncio.wait_for(loop.sock_accept(srv), timeout)
    data = b""
    while data.count(b"\n") < 2:
        c = await loop.sock_recv(conn, 65536)
        if not c:
            break
        data += c
    conn.close()
    _auth, user = [json.loads(x) for x in data.decode().strip().split("\n")]
    return wire.parse_wrapper(user["message"]["content"])


async def whoever_connects(candidates, run, timeout=1.0):
    """Run `run()` and report which of `candidates` ({label: server_socket})
    the ferry actually connected to."""
    loop = asyncio.get_running_loop()
    tasks = {label: asyncio.create_task(loop.sock_accept(srv))
             for label, srv in candidates.items()}
    await run()
    done, _ = await asyncio.wait(tasks.values(), timeout=timeout,
                                 return_when=asyncio.FIRST_COMPLETED)
    hit = [label for label, t in tasks.items() if t in done]
    for label, t in tasks.items():
        if t in done:
            t.result()[0].close()
        else:
            t.cancel()
    return hit


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
    d = Dedup(600000, lambda: 0); d.record("m1")  # prior successful delivery
    ferry = Ferry(cfg(tmp_path, short_sock_dir), lp, rec, d,
                  projectfn=lambda cwd: "PROJ", now_ms=lambda: 1)
    env = {"v": 1, "msg_id": "m1", "origin_host": "home", "project": "PROJ",
           "target": {"kind": "host", "host": "work", "project": "PROJ"},
           "type": "note", "ttl_s": 1, "created_at": 1, "orig_msg_id": None,
           "payload": {"body": "x", "hop_chain": None}}
    await ferry.on_deliver(env)
    assert rec.acks == ["m1"] and rec.posts == []
    lp.cleanup()


async def test_on_deliver_held_then_redelivered_is_not_lost(tmp_path, short_sock_dir):
    # Regression for: dedup recorded mid at check-time, so a redeliver of a
    # held message (no target session yet) was silently ack'd and dropped
    # without ever being injected once a session showed up.
    lp = LocalPeer("home", str(tmp_path), short_sock_dir, on_message=lambda f: None)
    lp.publish()
    rec = Recorder()
    ferry = Ferry(cfg(tmp_path, short_sock_dir), lp, rec, Dedup(600000, lambda: 0),
                  projectfn=lambda cwd: "PROJ", now_ms=lambda: 1)
    env = {"v": 1, "msg_id": "m1", "origin_host": "home", "project": "PROJ",
           "target": {"kind": "host", "host": "work", "project": "PROJ"},
           "type": "note", "ttl_s": 100000, "created_at": 1, "orig_msg_id": None,
           "payload": {"body": "run tests", "hop_chain": None}}

    # First delivery attempt: no matching session exists yet -> held, no ack.
    await ferry.on_deliver(env)
    assert rec.acks == [] and rec.posts == []

    # A matching session now appears (simulating the offline-then-replay
    # path), and the hub redelivers the same msg_id ~15s later.
    entry, sock, tok = make_session(tmp_path, short_sock_dir, 300, "iris-bg", "/repo")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock); srv.listen(2); srv.setblocking(False)

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


async def test_reply_goes_back_to_the_session_that_sent_the_note(
        tmp_path, short_sock_dir):
    # Regression: pick_target prefers `bg`, so a reply to a note sent from an
    # interactive session landed in an unrelated bg session on the same
    # project instead of the one actually waiting for the answer.
    inter, inter_sock, _ = make_session(tmp_path, short_sock_dir, 600, "iris-ac",
                                       "/repo", kind="interactive")
    _bg, bg_sock, _ = make_session(tmp_path, short_sock_dir, 601, "iris-bg",
                                   "/repo", kind="bg", status_updated_at=999)
    srv_inter, srv_bg = listen(inter_sock), listen(bg_sock)
    lp = LocalPeer("work", str(tmp_path), short_sock_dir, on_message=lambda f: None)
    lp.publish()
    rec = Recorder()
    ferry = Ferry(cfg(tmp_path, short_sock_dir), lp, rec, Dedup(600000, lambda: 0),
                  projectfn=lambda cwd: "PROJ", now_ms=lambda: 1)

    ferry.on_local_message({"raw_from": f"uds:{inter_sock}", "from_name": "iris-ac",
                            "hop_chain": None, "body": "review pr 42"})
    posted = rec.posts[0]["msg_id"]

    reply = {"v": 1, "msg_id": "r1", "origin_host": "home", "project": "PROJ",
             "target": {"kind": "host", "host": "work", "project": "PROJ"},
             "type": "reply", "ttl_s": 100000, "created_at": 2,
             "orig_msg_id": posted,
             "payload": {"body": "looks fine", "hop_chain": None}}
    hit = await whoever_connects({"interactive": srv_inter, "bg": srv_bg},
                                 lambda: ferry.on_deliver(reply))

    assert hit == ["interactive"], f"reply went to {hit}, not the asking session"
    assert rec.acks == ["r1"]
    lp.cleanup(); srv_inter.close(); srv_bg.close()


async def test_reply_held_while_origin_session_is_gone(tmp_path, short_sock_dir):
    # The asking session closes before the answer arrives. Holding (no ack) is
    # deliberate: the hub redelivers, and if that session comes back with the
    # same sessionId -- a new pid and socket, as on resume -- the answer still
    # reaches the session that asked for it.
    _inter, inter_sock, _ = make_session(tmp_path, short_sock_dir, 700, "iris-ac",
                                         "/repo", kind="interactive",
                                         session_id="sess-ac")
    _bg, bg_sock, _ = make_session(tmp_path, short_sock_dir, 701, "iris-bg",
                                   "/repo", kind="bg", status_updated_at=999)
    srv_bg = listen(bg_sock)
    lp = LocalPeer("work", str(tmp_path), short_sock_dir, on_message=lambda f: None)
    lp.publish()
    rec = Recorder()
    ferry = Ferry(cfg(tmp_path, short_sock_dir), lp, rec, Dedup(600000, lambda: 0),
                  projectfn=lambda cwd: "PROJ", now_ms=lambda: 1)

    ferry.on_local_message({"raw_from": f"uds:{inter_sock}", "from_name": "iris-ac",
                            "hop_chain": None, "body": "review pr 42"})
    posted = rec.posts[0]["msg_id"]
    reply = {"v": 1, "msg_id": "r1", "origin_host": "home", "project": "PROJ",
             "target": {"kind": "host", "host": "work", "project": "PROJ"},
             "type": "reply", "ttl_s": 100000, "created_at": 2,
             "orig_msg_id": posted,
             "payload": {"body": "looks fine", "hop_chain": None}}

    (tmp_path / "700.json").unlink()  # the asking session exits
    hit = await whoever_connects({"bg": srv_bg}, lambda: ferry.on_deliver(reply),
                                 timeout=0.3)
    assert hit == [], "held reply must not be delivered to a session that never asked"
    assert rec.acks == []

    # It resumes: same sessionId, new pid and socket. The hub redelivers.
    _again, again_sock, _ = make_session(tmp_path, short_sock_dir, 702, "iris-ac",
                                         "/repo", kind="interactive",
                                         session_id="sess-ac")
    srv_again = listen(again_sock)
    hit = await whoever_connects({"resumed": srv_again, "bg": srv_bg},
                                 lambda: ferry.on_deliver(reply))
    assert hit == ["resumed"]
    assert rec.acks == ["r1"]
    lp.cleanup(); srv_bg.close(); srv_again.close()


async def test_reply_without_a_pin_falls_back_to_pick_target(tmp_path, short_sock_dir):
    # The pin map is in-memory, so a ferry restart between the note and the
    # answer loses it. An unpinned reply must still be delivered on the old
    # heuristic rather than held forever.
    _bg, bg_sock, _ = make_session(tmp_path, short_sock_dir, 800, "iris-bg", "/repo")
    srv_bg = listen(bg_sock)
    lp = LocalPeer("work", str(tmp_path), short_sock_dir, on_message=lambda f: None)
    lp.publish()
    rec = Recorder()
    ferry = Ferry(cfg(tmp_path, short_sock_dir), lp, rec, Dedup(600000, lambda: 0),
                  projectfn=lambda cwd: "PROJ", now_ms=lambda: 1)

    reply = {"v": 1, "msg_id": "r1", "origin_host": "home", "project": "PROJ",
             "target": {"kind": "host", "host": "work", "project": "PROJ"},
             "type": "reply", "ttl_s": 100000, "created_at": 2,
             "orig_msg_id": "a-note-a-previous-ferry-posted",
             "payload": {"body": "looks fine", "hop_chain": None}}
    hit = await whoever_connects({"bg": srv_bg}, lambda: ferry.on_deliver(reply))
    assert hit == ["bg"]
    assert rec.acks == ["r1"]
    lp.cleanup(); srv_bg.close()


async def test_two_outstanding_notes_correlate_in_order(tmp_path, short_sock_dir):
    # Regression: _pending_reply held one msg_id per socket, so a second note
    # delivered before the first was answered overwrote the first's
    # correlation -- the first answer came back tagged as a reply to the
    # second note, and the second answer as an unrelated note.
    _e, sock, _t = make_session(tmp_path, short_sock_dir, 900, "iris-bg", "/repo")
    srv = listen(sock)
    lp = LocalPeer("work", str(tmp_path), short_sock_dir, on_message=lambda f: None)
    lp.publish()
    rec = Recorder()
    ferry = Ferry(cfg(tmp_path, short_sock_dir), lp, rec, Dedup(600000, lambda: 0),
                  projectfn=lambda cwd: "PROJ", now_ms=lambda: 1)

    def note(mid, body):
        return {"v": 1, "msg_id": mid, "origin_host": "home", "project": "PROJ",
                "target": {"kind": "host", "host": "work", "project": "PROJ"},
                "type": "note", "ttl_s": 100000, "created_at": 1,
                "orig_msg_id": None, "payload": {"body": body, "hop_chain": None}}

    for mid, body in (("note-a", "review pr 42"), ("note-b", "run the suite")):
        hit = await whoever_connects({"sess": srv}, lambda: ferry.on_deliver(note(mid, body)))
        assert hit == ["sess"]

    for body in ("pr 42 looks fine", "suite is green"):
        ferry.on_local_message({"raw_from": f"uds:{sock}", "from_name": "iris-bg",
                                "hop_chain": None, "body": body})

    assert [(p["type"], p["orig_msg_id"]) for p in rec.posts] == [
        ("reply", "note-a"), ("reply", "note-b")]
    lp.cleanup(); srv.close()


def test_pin_map_is_bounded_and_evicts_oldest(tmp_path, short_sock_dir, monkeypatch):
    # The pin map grows by one entry per note sent and is only drained when an
    # answer comes back. Answers that never come must not leak memory for the
    # life of the ferry.
    from c2c.ferry import app as app_mod
    monkeypatch.setattr(app_mod, "AWAITING_REPLY_MAX", 4)
    _e, sock, _t = make_session(tmp_path, short_sock_dir, 1000, "iris-x", "/repo")
    lp = LocalPeer("work", str(tmp_path), short_sock_dir, on_message=lambda f: None)
    lp.publish()
    rec = Recorder()
    ferry = Ferry(cfg(tmp_path, short_sock_dir), lp, rec, Dedup(600000, lambda: 0),
                  projectfn=lambda cwd: "PROJ", now_ms=lambda: 1)

    for i in range(6):
        ferry.on_local_message({"raw_from": f"uds:{sock}", "from_name": "iris-x",
                                "hop_chain": None, "body": f"task {i}"})
    posted = [p["msg_id"] for p in rec.posts]

    assert len(ferry._awaiting_reply) == 4
    assert posted[0] not in ferry._awaiting_reply  # oldest evicted
    assert posted[-1] in ferry._awaiting_reply     # newest kept
    lp.cleanup()


def test_reply_expires_sooner_than_a_note(tmp_path, short_sock_dir):
    # Holding a reply whose asking session is gone means the hub redelivers
    # until the envelope expires. A week of that for a session that is never
    # coming back is pointless; a note, which any project session can pick up,
    # still gets the full window.
    lp = LocalPeer("work", str(tmp_path), short_sock_dir, on_message=lambda f: None)
    ferry = Ferry(cfg(tmp_path, short_sock_dir), lp, Recorder(),
                  Dedup(600000, lambda: 0),
                  projectfn=lambda cwd: "PROJ", now_ms=lambda: 1)
    note = ferry.build_envelope("PROJ", "review pr 42", None, "note", None)
    reply = ferry.build_envelope("PROJ", "looks fine", None, "reply", "note-a")
    assert note["ttl_s"] == 604800
    assert reply["ttl_s"] == 3600


async def test_second_turn_of_an_exchange_also_returns_to_the_asker(
        tmp_path, short_sock_dir):
    # A follow-up from the asking session posts as a `reply` (it answers the
    # inbound one). If only notes get pinned, the answer to that follow-up
    # falls back to pick_target and the conversation jumps to the bg session
    # mid-exchange.
    _inter, inter_sock, _ = make_session(tmp_path, short_sock_dir, 1100, "iris-ac",
                                         "/repo", kind="interactive")
    _bg, bg_sock, _ = make_session(tmp_path, short_sock_dir, 1101, "iris-bg",
                                   "/repo", kind="bg", status_updated_at=999)
    srv_inter, srv_bg = listen(inter_sock), listen(bg_sock)
    lp = LocalPeer("work", str(tmp_path), short_sock_dir, on_message=lambda f: None)
    lp.publish()
    rec = Recorder()
    ferry = Ferry(cfg(tmp_path, short_sock_dir), lp, rec, Dedup(600000, lambda: 0),
                  projectfn=lambda cwd: "PROJ", now_ms=lambda: 1)

    def answer(mid, orig, body):
        return {"v": 1, "msg_id": mid, "origin_host": "home", "project": "PROJ",
                "target": {"kind": "host", "host": "work", "project": "PROJ"},
                "type": "reply", "ttl_s": 100000, "created_at": 2,
                "orig_msg_id": orig, "payload": {"body": body, "hop_chain": None}}

    # turn 1: asker asks, answer comes back to it
    ferry.on_local_message({"raw_from": f"uds:{inter_sock}", "from_name": "iris-ac",
                            "hop_chain": None, "body": "review pr 42"})
    hit = await whoever_connects({"asker": srv_inter, "bg": srv_bg},
                                 lambda: ferry.on_deliver(
                                     answer("a1", rec.posts[-1]["msg_id"], "two nits")))
    assert hit == ["asker"]

    # turn 2: asker follows up, and that answer must come back to it too
    ferry.on_local_message({"raw_from": f"uds:{inter_sock}", "from_name": "iris-ac",
                            "hop_chain": None, "body": "fix them please"})
    follow_up = rec.posts[-1]
    assert follow_up["type"] == "reply"  # it answers the inbound one
    hit = await whoever_connects({"asker": srv_inter, "bg": srv_bg},
                                 lambda: ferry.on_deliver(
                                     answer("a2", follow_up["msg_id"], "fixed")))
    assert hit == ["asker"], f"second turn went to {hit}"
    lp.cleanup(); srv_inter.close(); srv_bg.close()


async def test_hub_status_tells_the_asking_session_its_message_is_held(
        tmp_path, short_sock_dir):
    # Without this the message just sits in the hub's mailbox for a week and
    # nothing in the session that sent it ever says so.
    _e, sock, _t = make_session(tmp_path, short_sock_dir, 1200, "iris-ac",
                                "/repo", kind="interactive")
    srv = listen(sock)
    lp = LocalPeer("work", str(tmp_path), short_sock_dir, on_message=lambda f: None)
    lp.publish()
    rec = Recorder()
    ferry = Ferry(cfg(tmp_path, short_sock_dir), lp, rec, Dedup(600000, lambda: 0),
                  projectfn=lambda cwd: "gh/x/iris", now_ms=lambda: 1)

    ferry.on_local_message({"raw_from": f"uds:{sock}", "from_name": "iris-ac",
                            "hop_chain": None, "body": "review pr 42"})
    status = {"op": "status", "msg_id": rec.posts[0]["msg_id"],
              "state": "held_no_host", "host": "work", "project": "gh/x/iris"}

    task = asyncio.create_task(ferry.on_hub_status(status))
    got = await read_injected(srv)
    await task

    assert "work" in got["body"]
    assert "held" in got["body"].lower()
    # The heads-up is the ferry talking, not a peer message awaiting an answer:
    # recording it would make the session's next message look like a reply to
    # a note the ferry invented.
    assert ferry._pending_reply == {}
    lp.cleanup(); srv.close()


async def test_repeated_held_news_is_coalesced_then_repeats_after_cooldown(
        tmp_path, short_sock_dir):
    # Firing several messages at an offline host is one piece of news, not one
    # per message. It also has to start working again later, or a session that
    # hits this twice an hour only hears about it once.
    _e, sock, _t = make_session(tmp_path, short_sock_dir, 1300, "iris-ac",
                                "/repo", kind="interactive")
    srv = listen(sock)
    lp = LocalPeer("work", str(tmp_path), short_sock_dir, on_message=lambda f: None)
    lp.publish()
    rec = Recorder()
    clock = {"t": 1000}
    ferry = Ferry(cfg(tmp_path, short_sock_dir), lp, rec, Dedup(600000, lambda: 0),
                  projectfn=lambda cwd: "gh/x/iris", now_ms=lambda: clock["t"])

    async def send_and_report():
        ferry.on_local_message({"raw_from": f"uds:{sock}", "from_name": "iris-ac",
                                "hop_chain": None, "body": "ping"})
        await ferry.on_hub_status({"op": "status",
                                   "msg_id": rec.posts[-1]["msg_id"],
                                   "state": "held_no_host", "host": "work",
                                   "project": "gh/x/iris"})

    first = asyncio.create_task(read_injected(srv))
    await send_and_report()
    assert "work" in (await first)["body"]

    # second message, same news, same minute -> silence
    await send_and_report()
    with pytest.raises(asyncio.TimeoutError):
        await read_injected(srv, timeout=0.3)

    # an hour later it is news again
    clock["t"] += NOTIFY_COOLDOWN_MS + 1
    again = asyncio.create_task(read_injected(srv))
    await send_and_report()
    assert "work" in (await again)["body"]
    lp.cleanup(); srv.close()


async def test_sending_from_a_non_repo_directory_says_so(tmp_path, short_sock_dir):
    # Previously this was a log line and nothing else: the message vanished and
    # the session that sent it had no way to know why.
    _e, sock, _t = make_session(tmp_path, short_sock_dir, 1400, "scratch",
                                "/home/me/notes", kind="interactive")
    srv = listen(sock)
    lp = LocalPeer("work", str(tmp_path), short_sock_dir, on_message=lambda f: None)
    lp.publish()
    rec = Recorder()
    ferry = Ferry(cfg(tmp_path, short_sock_dir), lp, rec, Dedup(600000, lambda: 0),
                  projectfn=lambda cwd: None, now_ms=lambda: 1)

    reader = asyncio.create_task(read_injected(srv))
    ferry.on_local_message({"raw_from": f"uds:{sock}", "from_name": "scratch",
                            "hop_chain": None, "body": "review pr 42"})
    got = await reader

    assert rec.posts == []  # still not sent -- there is nowhere to send it
    assert "/home/me/notes" in got["body"]
    assert "origin" in got["body"]
    assert ferry._pending_reply == {}
    lp.cleanup(); srv.close()


async def test_an_unreachable_winner_does_not_starve_a_live_session(
        tmp_path, short_sock_dir):
    # A session can be alive yet unreachable: wedged, or a pid recycled under a
    # stale entry whose socket file still exists. Such a target has the
    # freshest statusUpdatedAt, so it wins selection, refuses the connection,
    # and -- because a failed inject holds without acking -- wins again on
    # every redelivery. The message never lands despite a live session
    # sitting right there.
    _a, dead_sock, _ = make_session(tmp_path, short_sock_dir, 1500, "wedged",
                                    "/repo", kind="bg", status_updated_at=999)
    pathlib.Path(dead_sock).write_text("")  # a path, but nothing accepting
    _b, good_sock, _ = make_session(tmp_path, short_sock_dir, 1501, "healthy",
                                    "/repo", kind="bg", status_updated_at=1)
    srv = listen(good_sock)
    lp = LocalPeer("work", str(tmp_path), short_sock_dir, on_message=lambda f: None)
    lp.publish()
    rec = Recorder()
    ferry = Ferry(cfg(tmp_path, short_sock_dir), lp, rec, Dedup(600000, lambda: 0),
                  projectfn=lambda cwd: "PROJ", now_ms=lambda: 1)

    env = {"v": 1, "msg_id": "m1", "origin_host": "home", "project": "PROJ",
           "target": {"kind": "host", "host": "work", "project": "PROJ"},
           "type": "note", "ttl_s": 100000, "created_at": 1,
           "orig_msg_id": None, "payload": {"body": "run tests", "hop_chain": None}}

    reader = asyncio.create_task(read_injected(srv, timeout=2))
    await ferry.on_deliver(env)
    got = await reader

    assert got["body"] == "run tests"
    assert rec.acks == ["m1"]
    # nothing is left pending against the target that refused us
    assert dead_sock not in ferry._pending_reply
    lp.cleanup(); srv.close()
