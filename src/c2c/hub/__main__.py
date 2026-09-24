"""Runnable c2c hub: `python -m c2c.hub ...`."""
from __future__ import annotations

import argparse
import asyncio
import logging
import ssl
import time

from c2c.hub.auth import load_auth
from c2c.hub.mailbox import Mailbox
from c2c.hub.server import Hub

log = logging.getLogger("c2c.hub")

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def build_hub(db_path: str, auth_path: str) -> tuple[Hub, Mailbox]:
    mb = Mailbox(db_path)
    auth = load_auth(auth_path)
    return Hub(mb, auth), mb


async def _sweeper(mb: Mailbox, interval_s: int) -> None:
    while True:
        await asyncio.sleep(interval_s)
        n = mb.sweep_expired(int(time.time() * 1000))
        if n:
            log.info("swept %d expired messages", n)


async def run(host, port, db_path, auth_path, sweep_interval_s=3600, ssl_context=None):
    if ssl_context is None and host not in _LOOPBACK_HOSTS:
        log.warning(
            "c2c hub is binding non-loopback host %s WITHOUT TLS: bearer "
            "tokens and envelope contents will be sent in cleartext. Put a "
            "TLS-terminating reverse proxy in front of this hub, or pass "
            "--certfile/--keyfile.",
            host,
        )
    hub, mb = build_hub(db_path, auth_path)
    server = await hub.serve(host, port, ssl_context=ssl_context)
    sweep = asyncio.create_task(_sweeper(mb, sweep_interval_s))
    log.info("c2c hub listening on %s:%s (tls=%s)", host, port, ssl_context is not None)
    try:
        await server.wait_closed()
    finally:
        sweep.cancel()
        mb.close()


def main() -> None:
    ap = argparse.ArgumentParser(prog="c2c.hub")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--db", required=True)
    ap.add_argument("--auth", required=True)
    ap.add_argument("--sweep-interval", type=int, default=3600)
    ap.add_argument("--certfile")
    ap.add_argument("--keyfile")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)

    ctx = None
    if args.certfile or args.keyfile:
        if not (args.certfile and args.keyfile):
            ap.error("--certfile and --keyfile must be given together")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(args.certfile, args.keyfile)

    asyncio.run(run(args.host, args.port, args.db, args.auth,
                    args.sweep_interval, ctx))


if __name__ == "__main__":
    main()
