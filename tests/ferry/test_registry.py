import hashlib
import json
import os
from c2c.ferry import registry as r


def write_session(d, pid, name, cwd, sock, kind="interactive", su=1000):
    entry = {
        "pid": pid, "name": name, "cwd": cwd, "kind": kind,
        "peerProtocol": 1, "version": "2.1.236",
        "messagingSocketPath": sock, "statusUpdatedAt": su,
    }
    (d / f"{pid}.json").write_text(json.dumps(entry))
    return entry


def test_read_sessions_only_pid_named(tmp_path):
    write_session(tmp_path, 100, "a", "/x", "/tmp/cc-socks/100.sock")
    (tmp_path / "not-a-pid.json").write_text('{"pid": 999}')
    pids = {s["pid"] for s in r.read_sessions(str(tmp_path))}
    assert pids == {100}


def test_session_by_socket_accepts_uri_and_path(tmp_path):
    write_session(tmp_path, 100, "a", "/x", "/tmp/cc-socks/100.sock")
    ss = r.read_sessions(str(tmp_path))
    assert r.session_by_socket(ss, "/tmp/cc-socks/100.sock")["pid"] == 100
    assert r.session_by_socket(ss, "uds:/tmp/cc-socks/100.sock")["pid"] == 100
    assert r.session_by_socket(ss, "/tmp/cc-socks/999.sock") is None


def test_pick_target_prefers_bg(tmp_path):
    write_session(tmp_path, 1, "i", "/repo", "/s1", kind="interactive", su=9000)
    write_session(tmp_path, 2, "b", "/repo", "/s2", kind="bg", su=1000)
    ss = r.read_sessions(str(tmp_path))
    pick = r.pick_target(ss, "PROJ", lambda cwd: "PROJ")
    assert pick["pid"] == 2  # bg wins even though interactive is more recent


def test_pick_target_most_recent_interactive_when_no_bg(tmp_path):
    write_session(tmp_path, 1, "old", "/repo", "/s1", su=1000)
    write_session(tmp_path, 2, "new", "/repo", "/s2", su=5000)
    ss = r.read_sessions(str(tmp_path))
    pick = r.pick_target(ss, "PROJ", lambda cwd: "PROJ")
    assert pick["pid"] == 2


def test_pick_target_filters_by_project(tmp_path):
    write_session(tmp_path, 1, "a", "/repoA", "/s1")
    write_session(tmp_path, 2, "b", "/repoB", "/s2")
    ss = r.read_sessions(str(tmp_path))
    proj = {"/repoA": "A", "/repoB": "B"}
    pick = r.pick_target(ss, "B", lambda cwd: proj[cwd])
    assert pick["pid"] == 2


def test_pick_target_none_when_no_match(tmp_path):
    write_session(tmp_path, 1, "a", "/repoA", "/s1")
    ss = r.read_sessions(str(tmp_path))
    assert r.pick_target(ss, "NOPE", lambda cwd: "A") is None


def test_pick_target_skips_protocol_mismatch(tmp_path):
    # A fresher protocol-2 session must not shadow an older protocol-1 one:
    # inject() only speaks protocol 1, so picking the protocol-2 session
    # would fail closed and wedge delivery even though a usable session
    # exists.
    old = write_session(tmp_path, 1, "old", "/repo", "/s1", su=1000)
    new = write_session(tmp_path, 2, "new", "/repo", "/s2", su=9000)
    new["peerProtocol"] = 2
    (tmp_path / "2.json").write_text(json.dumps(new))
    ss = r.read_sessions(str(tmp_path))
    pick = r.pick_target(ss, "PROJ", lambda cwd: "PROJ")
    assert pick["pid"] == 1


def test_pick_target_none_when_only_protocol_mismatch(tmp_path):
    entry = write_session(tmp_path, 1, "a", "/repo", "/s1")
    entry["peerProtocol"] = 2
    (tmp_path / "1.json").write_text(json.dumps(entry))
    ss = r.read_sessions(str(tmp_path))
    assert r.pick_target(ss, "PROJ", lambda cwd: "PROJ") is None


def test_peer_token_for_uses_no_realpath_hash(tmp_path):
    sock = "/tmp/cc-socks/100.sock"
    entry = write_session(tmp_path, 100, "a", "/x", sock)
    h = hashlib.sha256(sock.encode()).hexdigest()
    (tmp_path / f"100.{h}.key").write_text(json.dumps({"peerToken": "TOK"}))
    assert r.peer_token_for(entry, str(tmp_path)) == "TOK"


def test_peer_token_for_missing_key(tmp_path):
    entry = write_session(tmp_path, 100, "a", "/x", "/tmp/cc-socks/100.sock")
    assert r.peer_token_for(entry, str(tmp_path)) is None
