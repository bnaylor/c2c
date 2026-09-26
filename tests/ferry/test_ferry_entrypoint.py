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
