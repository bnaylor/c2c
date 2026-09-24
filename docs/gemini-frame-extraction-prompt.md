Extract and document the wire protocol used by Claude Code's local peer-to-peer
session messaging. This is interop work: I'm building a bridge daemon that
relays these messages between two of my own laptops, so I need the frame format
precisely enough to implement both halves of the protocol.

## Target

/opt/homebrew/Caskroom/claude-code/2.1.236/claude
  - 317 MB Mach-O 64-bit arm64 executable
  - a JS-runtime single-file executable with a bundled, minified JS graph inside

DO NOT try to read this file into context. It will not fit. Work with shell
tools: `strings -t d` to get byte offsets, `dd` to carve windows around those
offsets, then analyse the carved text.

## What I already know (do not re-derive; use as anchors)

Discovery: each live session writes ~/.claude/sessions/<pid>.json, e.g.
  {"pid":18506,"sessionId":"<uuid>","cwd":"/path","startedAt":<ms>,
   "procStart":"<ctime string>","version":"2.1.236","peerProtocol":1,
   "peerFeatures":["notify_idle"],"kind":"interactive","entrypoint":"cli",
   "messagingSocketPath":"/tmp/cc-socks/18506.sock","name":"docs-eb",
   "nameSource":"derived","nameSince":<ms>,"status":"busy",
   "updatedAt":<ms>,"statusUpdatedAt":<ms>}

Auth: a sibling file ~/.claude/sessions/<pid>.<sha256>.key, mode 0600:
  {"peerToken":"<32 hex chars>","procStart":"<ctime string>"}

Transport: one unix domain socket per session at /tmp/cc-socks/<pid>.sock,
mode 0600.

User-facing surface: two agent tools, ListAgents (enumerates peers) and
SendMessage (delivers to one peer; takes `to`, `message`, and `summary`).
A delivered message renders in the recipient's UI as
"Message from @<name> (ctrl-o to expand)".

## Method

1. Anchor hunt. `strings -t d` the binary and grep for, at minimum:
   peerToken, messagingSocketPath, peerProtocol, peerFeatures, notify_idle,
   cc-socks, statusUpdatedAt, nameSource, SendMessage, ListAgents.
   Also sweep for adjacent protocol vocabulary: hello, handshake, envelope,
   frame, ack, nack, deliver, inbox, subscribe, unauthorized, EPEERDOWN,
   and any "peer"-prefixed or "peer"-suffixed identifiers.
2. Carve. For each promising offset, `dd` a window (start with +/-64 KB, widen
   as needed) into a work file. Beautify the JS in that window before reading
   it -- minified single-letter identifiers are expected; rename them locally
   for your own analysis.
3. Read the code, not the strings. String hits only tell you where to look.
   The answer is in the socket server/client functions: whatever calls
   listen()/createServer() on messagingSocketPath, and whatever connects to it.
   Trace both.
4. If the JS graph turns out to be compressed and the carved windows are
   garbage, say so explicitly and stop -- report which offsets yielded
   unreadable data. Do not guess a protocol from string names alone.

## Deliverable

Write /Users/bnaylor/src/c2c/docs/peer-protocol-v1.md covering:

- Framing: exact byte-level structure. Length-prefixed or delimited? If
  length-prefixed: width, endianness, whether the prefix counts itself. If
  delimited: the delimiter and the escaping rule. Encoding (JSON? msgpack?).
  Max frame size if one is enforced.
- Handshake: the full open sequence from connect to ready. Which side speaks
  first, what the first frame contains, how peerProtocol version is negotiated
  or asserted, and what happens on a version mismatch.
- Auth: exactly where peerToken travels (which frame, which field), whether
  procStart is cross-checked, and what an auth failure looks like on the wire.
- Message types: the complete set. For each, the field names, types, which are
  required, and the expected reply. I specifically need the one SendMessage
  emits and the one ListAgents consumes (if ListAgents even uses the socket
  rather than just reading the registry -- determine which).
- Ack/error semantics: does the sender get delivery confirmation? What error
  codes or shapes exist? Behaviour when the recipient process is gone but the
  socket file remains.
- notify_idle: what this peerFeature actually does on the wire -- which frame
  requests it, what the notification frame looks like, and whether a queued
  message waits for idle or is delivered immediately regardless of `status`.
- Registry lifecycle: who writes/updates the session JSON and at what
  cadence, how `status` transitions, and how stale entries are reaped. This
  matters because my bridge must register itself convincingly as a peer.

## Rigour requirements

- Tag every claim OBSERVED (you read the code that does it, cite byte offset
  and the beautified snippet) or INFERRED (you are reasoning from names,
  structure, or convention). I will only implement against OBSERVED claims.
- Where you are uncertain between two readings, give both and say which
  experiment distinguishes them.
- Do not smooth the protocol into something tidier than the code. If it has
  legacy fields, dead branches, or inconsistent naming, document that.
- End with a verification plan: a minimal client script I can run against a
  live socket to confirm the handshake, and what a correct vs incorrect
  response looks like.

Note: this protocol is not a public contract and carries no compatibility
guarantee, so flag anything that looks version-fragile and worth gating on the
`version` / `peerProtocol` fields in the registry.
