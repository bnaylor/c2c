from c2c.ferry.__main__ import build_ferry
from c2c.ferry.config import FerryConfig


def test_build_ferry_wires_callbacks(tmp_path):
    (tmp_path / "socks").mkdir()
    cfg = FerryConfig(host_id="home", peer_host="work",
                      hub_url="ws://x", token="t",
                      sessions_dir=str(tmp_path), sock_dir=str(tmp_path / "socks"))
    ferry, peer, hub, dedup = build_ferry(cfg)
    assert peer.name == "work"                 # peer named after the other host
    assert hub._on_deliver == ferry.on_deliver
    assert hub._projects == ferry.live_projects
    assert peer._on_message == ferry.on_local_message
    # without this the hub's held-message reports are silently discarded
    assert hub._on_status == ferry.on_hub_status


def test_sigterm_removes_the_ferrys_registry_artifacts(tmp_path):
    """A restart that leaves the entry, key file and socket behind puts a
    second peer with the same name in the registry. SIGTERM is catchable, so
    the ordinary restart path should clean up after itself."""
    import json, os, signal, subprocess, sys, tempfile, time, glob, shutil

    socks = tempfile.mkdtemp(prefix="c2c-t-", dir="/tmp")
    sessions = tmp_path / "sessions"; sessions.mkdir()
    conf = tmp_path / "ferry.json"
    conf.write_text(json.dumps({
        "host_id": "home", "peer_host": "work",
        "hub_url": "ws://127.0.0.1:1",  # nothing there; the link just retries
        "token": "t", "sessions_dir": str(sessions), "sock_dir": socks,
    }))
    os.chmod(conf, 0o600)
    proc = subprocess.Popen(
        [sys.executable, "-m", "c2c.ferry", "--config", str(conf)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 10
        entry = sessions / f"{proc.pid}.json"
        while time.time() < deadline and not entry.exists():
            time.sleep(0.05)
        assert entry.exists(), "ferry never published its registry entry"
        assert glob.glob(str(sessions / f"{proc.pid}.*.key"))
        assert os.path.exists(f"{socks}/{proc.pid}.sock")

        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) is not None

        assert not entry.exists(), "registry entry outlived the process"
        assert not glob.glob(str(sessions / f"{proc.pid}.*.key")), "key file left behind"
        assert not os.path.exists(f"{socks}/{proc.pid}.sock"), "socket left behind"
    finally:
        if proc.poll() is None:
            proc.kill(); proc.wait(timeout=5)
        shutil.rmtree(socks, ignore_errors=True)
