import copy
from c2c.hub.mailbox import Mailbox


def env(msg_id, project, origin, target, created=1000):
    return {
        "v": 1, "msg_id": msg_id, "origin_host": origin, "project": project,
        "target": target, "type": "note", "ttl_s": 10,  # expires at created+10000
        "created_at": created, "orig_msg_id": None, "payload": {},
    }


def proj_target(project):
    return {"kind": "project", "project": project}


def host_target(host, project):
    return {"kind": "host", "host": host, "project": project}


def test_directed_delivered_to_target_host_only(tmp_path):
    m = Mailbox(str(tmp_path / "m.db"))
    m.put(env("a", "P", "home", host_target("work", "P")))
    assert [e["msg_id"] for e in m.pending_for("work", {"P"}, 2000)] == ["a"]
    assert m.pending_for("laptop", {"P"}, 2000) == []
    m.close()


def test_fanout_excludes_origin(tmp_path):
    m = Mailbox(str(tmp_path / "m.db"))
    m.put(env("a", "P", "home", proj_target("P")))
    assert m.pending_for("home", {"P"}, 2000) == []          # origin excluded
    assert [e["msg_id"] for e in m.pending_for("work", {"P"}, 2000)] == ["a"]
    m.close()


def test_fanout_requires_host_has_project(tmp_path):
    m = Mailbox(str(tmp_path / "m.db"))
    m.put(env("a", "P", "home", proj_target("P")))
    assert m.pending_for("work", {"OTHER"}, 2000) == []
    assert [e["msg_id"] for e in m.pending_for("work", {"P"}, 2000)] == ["a"]
    m.close()


def test_ack_stops_redelivery(tmp_path):
    m = Mailbox(str(tmp_path / "m.db"))
    m.put(env("a", "P", "home", host_target("work", "P")))
    m.ack("a", "work")
    assert m.pending_for("work", {"P"}, 2000) == []
    m.close()


def test_ack_is_per_host(tmp_path):
    m = Mailbox(str(tmp_path / "m.db"))
    m.put(env("a", "P", "home", proj_target("P")))
    m.ack("a", "work")
    assert m.pending_for("work", {"P"}, 2000) == []
    assert [e["msg_id"] for e in m.pending_for("laptop", {"P"}, 2000)] == ["a"]
    m.close()


def test_expired_not_delivered(tmp_path):
    m = Mailbox(str(tmp_path / "m.db"))
    m.put(env("a", "P", "home", host_target("work", "P"), created=1000))
    # expires at 1000 + 10*1000 = 11000
    assert m.pending_for("work", {"P"}, 12000) == []
    assert [e["msg_id"] for e in m.pending_for("work", {"P"}, 5000)] == ["a"]
    m.close()


def test_put_idempotent_on_msg_id(tmp_path):
    m = Mailbox(str(tmp_path / "m.db"))
    m.put(env("a", "P", "home", host_target("work", "P")))
    m.put(env("a", "P", "home", host_target("work", "P")))  # no raise, no dup
    assert len(m.pending_for("work", {"P"}, 2000)) == 1
    m.close()


def test_returned_envelope_is_full(tmp_path):
    m = Mailbox(str(tmp_path / "m.db"))
    original = env("a", "P", "home", host_target("work", "P"))
    original["payload"] = {"pr": 42}
    m.put(copy.deepcopy(original))
    got = m.pending_for("work", {"P"}, 2000)[0]
    assert got == original
    m.close()


def test_ordered_oldest_first(tmp_path):
    m = Mailbox(str(tmp_path / "m.db"))
    m.put(env("new", "P", "home", host_target("work", "P"), created=3000))
    m.put(env("old", "P", "home", host_target("work", "P"), created=1000))
    assert [e["msg_id"] for e in m.pending_for("work", {"P"}, 5000)] == ["old", "new"]
    m.close()
