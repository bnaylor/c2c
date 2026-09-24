import json
import os
from c2c.hub.__main__ import build_hub


def test_build_hub_wires_components(tmp_path):
    ap = tmp_path / "auth.json"
    ap.write_text(json.dumps({"hosts": {"home": "t1"}}))
    os.chmod(ap, 0o600)
    hub, mb = build_hub(str(tmp_path / "h.db"), str(ap))
    assert hub._auth.verify("t1") == "home"
    mb.close()
