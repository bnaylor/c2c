from c2c.hub.mailbox import Mailbox


def env(msg_id, project="P", created=1000, ttl=10):
    return {
        "v": 1, "msg_id": msg_id, "origin_host": "home", "project": project,
        "target": {"kind": "project", "project": project},
        "type": "note", "ttl_s": ttl, "created_at": created,
        "orig_msg_id": None, "payload": {},
    }


def test_sweep_deletes_expired_and_returns_count(tmp_path):
    m = Mailbox(str(tmp_path / "m.db"))
    m.put(env("a", created=1000, ttl=10))   # expires 11000
    m.put(env("b", created=1000, ttl=100))  # expires 101000
    assert m.sweep_expired(12000) == 1
    assert [e["msg_id"] for e in m.pending_for("work", {"P"}, 12000)] == ["b"]
    m.close()


def test_sweep_also_clears_acks(tmp_path):
    m = Mailbox(str(tmp_path / "m.db"))
    m.put(env("a", created=1000, ttl=10))
    m.ack("a", "work")
    m.sweep_expired(12000)
    # reinserting the same id after sweep behaves like a fresh message
    m.put(env("a", created=1000, ttl=1000))  # expires 1001000
    assert [e["msg_id"] for e in m.pending_for("work", {"P"}, 2000)] == ["a"]
    m.close()


def test_per_project_cap_evicts_oldest(tmp_path):
    m = Mailbox(str(tmp_path / "m.db"), max_per_project=2)
    m.put(env("old", created=1000, ttl=100000))
    m.put(env("mid", created=2000, ttl=100000))
    m.put(env("new", created=3000, ttl=100000))  # over cap -> evict "old"
    ids = [e["msg_id"] for e in m.pending_for("work", {"P"}, 5000)]
    assert ids == ["mid", "new"]
    m.close()


def test_cap_is_per_project(tmp_path):
    m = Mailbox(str(tmp_path / "m.db"), max_per_project=1)
    m.put(env("p1", project="P", created=1000, ttl=100000))
    m.put(env("q1", project="Q", created=1000, ttl=100000))
    m.put(env("p2", project="P", created=2000, ttl=100000))  # evicts p1, not q1
    p_ids = [e["msg_id"] for e in m.pending_for("work", {"P"}, 5000)]
    q_ids = [e["msg_id"] for e in m.pending_for("work", {"Q"}, 5000)]
    assert p_ids == ["p2"] and q_ids == ["q1"]
    m.close()


def test_directed_delivered_even_if_host_lacks_project(tmp_path):
    # A host-directed message must reach its target host regardless of
    # whether that host currently has the project announced (e.g.
    # delegating "work:iris" to an idle box must not be black-holed).
    m = Mailbox(str(tmp_path / "m.db"))
    m.put({
        "v": 1, "msg_id": "d1", "origin_host": "home", "project": "iris",
        "target": {"kind": "host", "host": "work", "project": "iris"},
        "type": "note", "ttl_s": 10000, "created_at": 1000,
        "orig_msg_id": None, "payload": {},
    })
    ids = [e["msg_id"] for e in m.pending_for("work", {"P", "Q"}, 2000)]
    assert ids == ["d1"]
    m.close()
