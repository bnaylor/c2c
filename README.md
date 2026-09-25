# c2c -- Claude to Claude

Claude Code already lets sessions on the *same* machine find each other and pass messages around.  c2c extends that across machines, so a session on your laptop can hand work to a session on another box and get an answer back -- without you copy-pasting between two terminals.

## Why you'd want this

Concrete version of the problem: I run Claude Code on two laptops.  One is personal, on a metered plan.  The other is a work machine wired to  Anthropic models on Vertex.  Plenty of projects live on both.  Today, coordinating them is manual -- I notice a PR needs review, walk over to the other laptop, paste the request in, wait, walk back.  Same for "run the integration suite on your box" and "you take this issue, I'll take that one".

c2c makes that automatic.  From a session on the home box you send a one-liner to the work box; a background agent over there wakes up, does the review (or runs the suite, or picks up the issue), and reports back into your session.  The expensive work happens where the budget is, and you never touched the other keyboard.

The payoff, in one sentence: your fleet of Claude sessions becomes one coordinated thing instead of N isolated ones, and the heavy lifting lands wherever you want it to.

A nice side effect: the channel is completely out-of-band from the model API.  It doesn't care that one host talks to Anthropic and the other talks to Vertex, or that they're on different accounts.  It's just sockets and a relay.

## How it works

Two pieces plus your existing sessions:

```
 HOST A (home)              HUB (public)              HOST B (work)
  claude sessions          per-project               claude sessions
     |  SendMessage        mailboxes (durable)          ^
  [ ferry ] <==== wss/tls ==> token auth + route <==== wss/tls ==> [ ferry ]
```

- **ferry** -- a small daemon on each host.  It publishes itself into Claude Code's local session registry, so it shows up as an ordinary peer in `ListAgents` and answers to `SendMessage` like any other session.  On the receiving side it injects incoming messages into a live local session (preferring a background one) using the same peer protocol real sessions use.
- **hub** -- one dumb, durable relay you host somewhere both machines can reach.  It holds per-project mailboxes, authenticates each host with a token, and forwards messages.  It stores and forwards, so the far side doesn't have to be awake when you send -- the message waits until a session that can handle it shows up.

Projects are keyed by git remote URL, so "the iris project" means the same thing on both hosts automatically.  You don't configure a project list; the ferry figures it out from whatever repos your sessions are sitting in.

Messages arrive as the normal `Message from @work (ctrl+o to expand)` line you already see for same-machine peers, and the agent's response shows up in its transcript.  Nothing happens silently -- it's the exact same surface Claude Code already gives you, just sourced from another machine.

It rides a protocol that isn't officially documented.  We reverse-engineered it and verified every claim on the wire (see `docs/protocol.md` and `tap.py` if you're curious -- the short version is that a parked background session really will wake on an inbound message, act on it, and report back, which is the whole thing that makes this work).

## Quickstart

Prereqs: Python 3.11+ on each host, and somewhere to run the hub that both hosts can reach over TLS.  Both hosts need the same repo checked out with the same `origin` remote.

**1. Install** (on each host, and wherever the hub runs):

```
pip install -e .
```

**2. Make some tokens** -- one per host, any random hex is fine:

```
python3 -c "import secrets; print(secrets.token_hex(16))"
```

**3. Start the hub** (on the public box).  Auth file, mode 0600:

```json
// hub-auth.json
{ "hosts": { "home": "<home-token>", "work": "<work-token>" } }
```

```
chmod 600 hub-auth.json
python -m c2c.hub --db hub.db --auth hub-auth.json \
    --certfile cert.pem --keyfile key.pem
```

TLS is not optional -- the tokens are bearer credentials and go over the wire.  If you'd rather terminate TLS at a reverse proxy (nginx/caddy/etc.), point the proxy at a hub bound to localhost and skip `--certfile/--keyfile`.  Run it without TLS on a non-loopback interface and it'll warn you, loudly, every time.

**4. Start a ferry** on each host.  Config, mode 0600:

```json
// ferry-home.json  (on the home box)
{ "host_id": "home", "peer_host": "work",
  "hub_url": "wss://your-hub:8765", "token": "<home-token>" }
```

```
chmod 600 ferry-home.json
python -m c2c.ferry --config ferry-home.json
```

Do the mirror image on the work box (`host_id: work`, `peer_host: home`, its own token).

**5. Use it.**  Open a Claude Code session on a shared repo on the home box.  The work host now appears as a peer named `work`:

```
ListAgents                      # you'll see `work` in the list
SendMessage(to="work", message="review PR #42 and reply with findings")
```

The ferry infers the project from your session's working directory, drops the message in the hub, and the work box's ferry delivers it into a session there.  You get the reply back in your session.  For it to land immediately the far side needs a session that's actually taking turns -- a background agent (`claude --bg "..."`) is the ideal target, since it sits alive and picks up inbound messages on its own.  If nothing's running for that project yet, the hub holds the message until something is.

## What you can send (v1)

Freeform coordination: `note` and `reply`.  You write the instruction, the receiving agent reads it and acts.  "Review PR #42."  "Run the integration suite and tell me what breaks."  "I'm taking the auth refactor, leave it alone."

Typed requests (`review_request`, `test_request`, `assign_issue`) are wired through the protocol but not yet surfaced as first-class UX -- that's the next thing.  For now a plain note does the job.

## Status

This is v1, and honest about it:

- Built and tested for **one person, two hosts**, all sessions in auto permission mode.  That's the design center, not a limitation to apologize for.
- The hub must sit behind TLS.  In-process rate limiting isn't there -- put it behind a proxy if it's exposed to the open internet.
- The ferry's outbound queue is in-memory.  A ferry restart mid-flight recovers via hub redelivery + dedup, so you get at-least-once, not lost messages -- but not zero duplicates across a crash.
- 3+ hosts and the typed message kinds are follow-ups.  The two-host case is a single peer per side, which keeps everything simple.

Not a hosted service, not multi-tenant, no accounts.  It's your machines talking to each other through a relay you control.

## Layout

- `src/c2c/hub/` -- the relay: sqlite mailbox, token auth, websocket server
- `src/c2c/ferry/` -- the per-host daemon: wire adapter, registry reader, hub client, orchestrator
- `docs/protocol.md` -- the reverse-engineered peer protocol, verified on the wire
- `docs/superpowers/` -- the design spec and implementation plans
- `tap.py` -- the probe used to capture real frames (handy for poking at the protocol yourself)
- `tests/` -- unit tests per module plus an end-to-end test that stands up a real hub, two ferries, and a fake background session and pushes a message through the whole thing
