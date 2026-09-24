"""Runnable c2c ferry: `python -m c2c.ferry --config ferry.json`."""
from __future__ import annotations

import argparse
import asyncio
import logging
import time

from c2c.ferry.app import Ferry
from c2c.ferry.config import FerryConfig, load_config
from c2c.ferry.dedup import Dedup
from c2c.ferry.hubclient import HubClient
from c2c.ferry.localpeer import LocalPeer

log = logging.getLogger("c2c.ferry")


def build_ferry(config: FerryConfig):
    dedup = Dedup(config.dedup_window_ms, lambda: int(time.time() * 1000))

    # Placeholder callbacks to break circular dependency during construction
    def _noop_deliver(env):
        pass
    def _noop_local(fields):
        pass
    def _noop_projects():
        return []

    peer = LocalPeer(config.peer_name, config.sessions_dir, config.sock_dir, _noop_local)
    hub = HubClient(config.hub_url, config.token, _noop_projects, _noop_deliver)
    ferry = Ferry(config, peer, hub, dedup)

    # Wire the actual callbacks after ferry is created
    hub._on_deliver = ferry.on_deliver
    hub._projects = ferry.live_projects
    peer._on_message = ferry.on_local_message

    return ferry, peer, hub, dedup


async def _announcer(ferry: Ferry, hub: HubClient, interval_s: int = 15) -> None:
    last: list[str] = []
    while True:
        await asyncio.sleep(interval_s)
        cur = ferry.live_projects()
        if cur != last:
            hub.announce(cur)
            last = cur


async def run(config: FerryConfig) -> None:
    ferry, peer, hub, _ = build_ferry(config)
    peer.publish()
    log.info("ferry up: peer=%s host=%s hub=%s", peer.name, config.host_id, config.hub_url)
    serve = asyncio.create_task(peer.serve())
    link = asyncio.create_task(hub.run())
    ann = asyncio.create_task(_announcer(ferry, hub))
    try:
        await asyncio.gather(serve, link, ann)
    finally:
        for t in (serve, link, ann):
            t.cancel()
        peer.cleanup()


def main() -> None:
    ap = argparse.ArgumentParser(prog="c2c.ferry")
    ap.add_argument("--config", required=True)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(load_config(args.config)))


if __name__ == "__main__":
    main()
