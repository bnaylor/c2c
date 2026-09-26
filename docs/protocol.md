# Claude Code peer messaging — reconciled protocol

Single source of truth for c2c. Merges three inputs:

- `peer-messaging-observed.md` — filesystem facts read off a live install
- `peer-protocol-v1.md` — Gemini's binary extraction (detail + provenance)
- `peer-protocol-v1.md` §10 — post-extraction corrections

Confidence tags: **[OBS]** confirmed against the live system here ·
**[SRC]** read directly out of the shipped binary's bundled JS — the control
flow itself, not a summary of it; ranks above [EXT] because nothing is
paraphrasing, and it can settle negatives a wire tap cannot (an absent UI
handler emits no frame to capture) · **[EXT]** from binary extraction,
consistent with outside observation, not yet seen on the wire · **[INF]**
inference, unverified · **[WIRE]** to be confirmed by the socket tap
(`tap.py`).

[SRC] findings cite the minified symbol names they came from. Those names are
regenerated per build: treat them as a re-verification trail for this exact
install, not as stable identifiers.

Install observed: claude 2.1.236, macOS arm64, 2026-09-23.
None of this is a public contract. Gate everything on the registry's
`version` / `peerProtocol` fields and expect it to move between releases.

---

## Model in one paragraph

Sessions on one host find each other by scanning a registry directory, and
message each other over one Unix domain socket per session. A sender reads the
target's registry entry, derives the target's key filename from its socket
path, reads a shared token from that key file, connects to the socket, and
writes newline-delimited JSON: an auth line, then a payload line. There is no
synchronous ack. Delivery receipts and idle notifications come back over a
*new reverse connection* to the sender's own socket. It is all same-uid,
local-filesystem; account and model-hosting are irrelevant to it.

---

## Three artifacts per session

### 1. Registry entry — discovery **[OBS]**

`~/.claude/sessions/<pid>.json`, mode 0644:

```json
{"pid":18506,"sessionId":"69ab1147-…","cwd":"/Users/bnaylor/src/c2c/docs",
 "startedAt":1790209842330,"procStart":"Thu Sep 24 00:30:39 2026",
 "version":"2.1.236","peerProtocol":1,"peerFeatures":["notify_idle"],
 "kind":"interactive","entrypoint":"cli",
 "messagingSocketPath":"/tmp/cc-socks/18506.sock",
 "name":"docs-eb","nameSource":"derived","nameSince":1790209842330,
 "status":"busy","updatedAt":1790210291045,"statusUpdatedAt":1790210291045}
```

- `name` is the `SendMessage` address; derived from `cwd` basename + suffix. **[OBS]**
- `status` observed: `busy`, `idle`. Extraction adds `shell`, `waiting`. **[OBS]** / **[INF]**
- `kind` observed: `interactive`, `bg`. Extraction adds `daemon`, `daemon-worker`. **[OBS]** / **[INF]**
- A peer with `peerProtocol < 1` is filtered out of the messageable list. **[EXT]**

### 2. Key file — auth **[OBS]**

`~/.claude/sessions/<pid>.<hash>.key`, mode 0600:

```json
{"peerToken":"9d3b3237459f372ba9e495bfd2b80a94","procStart":"Thu Sep 24 00:30:39 2026"}
```

- `peerToken` = 16 random bytes as hex (32 chars).
- **`<hash>` = `sha256(messagingSocketPath)` using the path exactly as written
  in the registry — NOT realpath-canonicalized.** **[OBS]**

  This corrects the extraction. On macOS `/tmp` → `private/tmp`, so it matters:
  ```
  sha256("/tmp/cc-socks/18506.sock")         = 4391eeaf…  ← real filename
  sha256("/private/tmp/cc-socks/18506.sock") = a384b872…  ← wrong
  ```
  Verified across all five live sessions. A sender derives this filename
  itself, so a bridge/receiver MUST publish its key under the same
  no-realpath hash or no one finds its token.

### 3. Socket — transport **[OBS]**

`/tmp/cc-socks/<pid>.sock`, `srw-------` (0600), inside `cc-socks/` (0700).
Exactly one per live registry entry.

Extraction claims a subprocess also gets `CLAUDE_CODE_MESSAGING_SOCKET` +
`CLAUDE_CODE_MESSAGING_TOKEN` (a separate `childToken`) in its env, allowing a
child to authenticate without reading a key file. **[EXT]** — unverified;
`printenv CLAUDE_CODE_MESSAGING_SOCKET` inside a real session would confirm.

---

## Wire format **[EXT → partly OBS]**

Everything in this section is from extraction, internally consistent, and
contradicted by nothing observable — but no frame has been seen yet. `tap.py`
is what turns these into **[OBS]**.

**Framing:** newline-delimited JSON (NDJSON), UTF-8, one JSON object per line.
Hard cap 1 MiB per line; the server drops the connection on overflow.

**Handshake:** client speaks first, no server banner. Line 1 = auth, line 2 =
payload, then the client half-closes the write side. macOS client waits ~150 ms
before `end()` to let the socket buffer flush.

**Auth frame (line 1):**
```json
{"type":"auth","token":"<32 hex>"}
```
Server compares with `timingSafeEqual` against `peerToken` (→ role `peer`) or
`childToken` (→ role `child`). Failure = silent `socket.destroy()`, no error
frame. **[EXT]**

**Payload envelope (line 2+):** `msgV:1`, `msg_id` (uuid v4), `type`
(`user`|`control`), optional `session_id` (dropped if it doesn't match the
recipient).

`user` — what `SendMessage` emits:
```json
{"msgV":1,"msg_id":"<uuid>","type":"user","priority":"next",
 "from":"uds:/tmp/cc-socks/18506.sock",
 "message":{"role":"user","content":"<cross-session-message …>…</cross-session-message>"}}
```
`priority` ∈ `next`(default)|`now`|`later`. `from` is the sender's socket as a
**plain (not url-encoded)** `uds:` URI — confirmed on the wire, correcting the
extraction. Receipts come back by connecting to it. The `content` wraps the text:
```
<cross-session-message from="uds:…" from-name="…" from-mode="prompting">
…text…
</cross-session-message>
```
Literal `<cross-session-message` in user text is escaped `<\`.

**Control frames** (each its own line, `type:"control"`). The frame's
discriminator is **`action`**, not `control` — corrected from extraction,
**[SRC]** (`O8m` dispatches on `e.action`; `KKo` builds
`{type:"control", ...frame, msgV, msg_id}`):
- `notify_when_idle` — subscribe; carries `from` to be called back on.
- `peer_idle_notice` — sent back on reverse connection; `state` ∈
  `idle`|`exited`|`unavailable`, `orig_msg_id`, optional `finished_at`/`detail`.
- `peer_message_status` — sent back; `status` ∈
  `held`|`delivered`|`denied`|`expired`, `orig_msg_id`, optional `reason`.
  **Not user-visible, and `reason` is discarded on receipt** — see
  "peer_message_status is invisible to the sender" below before building on it.
- `rename` — assigns a new `name`.

Any control frame carrying a `session_id` that doesn't equal the recipient's
own is dropped before dispatch (`$8m`). **[SRC]**

**Acks:** none synchronous. `write()` success + clean close = "sent". All
feedback is a later reverse connection to the sender's `from`. **[EXT]**

**Stale socket:** dead process, socket file lingering → `ECONNREFUSED`/`ENOENT`,
surfaced as "peer may have restarted, call ListAgents". **[EXT]**

**`ListAgents` does not use the wire.** It reads the registry and does a 250 ms
connect probe writing zero bytes. **[EXT]**

**Peer-pid check:** for control sends with `expectPeerPid`, the client checks
the connected socket's `SO_PEERCRED` pid against the target and aborts on
mismatch. Whether plain `user` sends also do this is UNKNOWN and matters for
the bridge — `tap.py`'s forwarding variant is how we'd find out. **[INF]**

---

## What the tap must confirm

Ranked by how much they change the bridge design:

1. **Does a `user` send peer-pid-check the receiver?** If yes, a bridge posing
   as a peer needs its listener in the process whose pid is in the registry —
   no forking it to a helper.
2. ~~**Does inbound trigger a `held` approval prompt?**~~ ANSWERED (capture #2):
   NOT held in auto mode — delivered automatically. Stricter modes still open.
3. **Exact framing + auth acceptance** — the `tap.py` fake receiver settles
   this directly and doubles as the bridge's first proof of concept.
4. Registry update cadence and who reaps stale entries. The extraction's
   `.fleetview-heartbeat` (5 s touch) is **refuted** — no such file exists with
   five sessions live. Cadence still UNKNOWN. **[OBS-negative]**

---

## Implications for c2c (unchanged by the merge)

- Channel is out-of-band from the model API → Anthropic-vs-Vertex split is moot.
- Bridge poses as a local session per host: write registry + key + socket,
  relay to the peer host. Discovery, addressing, and UX come free.
- Both halves needed: serve a socket (receive from local) and connect to peer
  sockets (deliver inbound).
- Namespace names — `cwd`-derived names collide across hosts (`iris-ac` twice).
- Fail closed on `version`/`peerProtocol` drift.
- Cross-host recipients are usually offline → relay needs durable
  store-and-forward, keyed on the GitHub URL, replaying when a matching-repo
  session appears.

---

## Wire capture — 2026-09-23 (tap.py, sink mode)

First real frames, from `SendMessage(to="tap-sink", …)` sent by session
`c2c-d5` (`/tmp/cc-socks/25002.sock`). Raw log in `tap.log`.

**Confirmed [OBS]:**

- **No-realpath key hash, end to end.** Sender derived the key filename from
  the socket path as written, read our published `peerToken`, and returned it
  verbatim in the auth frame. Gemini's `realpath()` claim is refuted on the
  wire, not just by filename math.
- **Auth then payload, one connection, NDJSON.** Line 1
  `{"type":"auth","token":"…"}`, line 2 the `user` envelope. Client wrote
  first, we sent nothing back, delivery still completed. 379 bytes total.
- **`SendMessage` probes then sends.** Two connections at the same second: a
  zero-byte liveness probe, then a second connection carrying the frames.
  Earlier zero-byte connection was a separate `ListAgents`. Confirms
  registry-read + zero-byte connect probing, and adds: the send path does its
  own probe before delivering.

**Corrected [OBS], superseding extraction:**

- `from` is plain, `uds:/tmp/cc-socks/25002.sock` — NOT url-encoded.
- Wrapper attributes are `from`, `from-name`, `from-mode` only. No
  `from-session`. Observed wrapper:
  ```
  <cross-session-message from="uds:/tmp/cc-socks/25002.sock" from-name="c2c-d5" from-mode="prompting">
  hello from the tap
  </cross-session-message>
  ```
- Observed envelope (optional fields `session_id`, `file_attachments`, `uuid`
  all omitted by the sender):
  ```json
  {"msgV":1,"msg_id":"35763e4a-f630-436a-a219-006256631515","type":"user",
   "message":{"role":"user","content":"<cross-session-message …>…</cross-session-message>"},
   "priority":"next","from":"uds:/tmp/cc-socks/25002.sock"}
  ```

**Still UNKNOWN after this run (both need the forwarding variant / a real
receiver — the sink cannot test them):**

1. **Peer-pid check on `user` sends.** Untestable in sink mode: our listener's
   pid equals the registry pid, so any `expectPeerPid` check passes trivially.
   Needs a proxy whose pid ≠ the target's.
2. **`held` approval prompt.** The hold is a receiver-side policy; a policyless
   sink holds nothing. Needs a real Claude session as recipient.
3. macOS gives no `SO_PEERCRED`, so the tap can't read the connecting pid at
   all — a forwarding proxy would have to log the sender's identity another way.

**Bridge takeaway:** posing as a peer works. A pure Python process with the
three artifacts received a real cross-session message and authenticated a real
sender. The receive half of the bridge is proven; the send half and the
approval-policy question are what's left.

---

## Wire capture #2 — 2026-09-23 (tap.py, send mode + --notify)

`tap.py --to trois-bocaux-50 --notify`. Target was a real session in
`--permission-mode auto`. Raw log in `tap.log`. This settles the reverse
channel and the hold question, and surfaces a new field.

**Hold question — ANSWERED for auto mode [OBS]:**
Inbound `user` message was **delivered, not held**. Target rendered it and the
send was "Allowed by auto mode classifier" — no user-approval prompt. So under
auto permission mode, cross-host relay can run unattended. The `held` path and
`peer_message_status` never fired. Behaviour under stricter permission modes is
still UNKNOWN and may differ (the `held` + permission-parity path presumably
lives there).

**Reverse channel — CONFIRMED [OBS]:**
- Receiver delivers feedback by opening a NEW connection to the sender's `from`
  socket and sending `auth` + frame.
- **The reverse auth uses the SENDER's peerToken.** The target read our key
  file — deriving its name by hashing our `from` socket path with **no
  realpath** — and authed back with our token (`[in 1]` token = our published
  token). Confirms the no-realpath hash bidirectionally and means a bridge's
  `from` socket MUST publish a discoverable, correctly-hashed key file or it
  gets no callbacks.
- An agent's ack is a **plain `user` frame in reverse**, not a distinct ack
  type. `peer_message_status` exists only on the (unseen) hold path.
- `peer_idle_notice` round-tripped: `state:"idle"`, `finished_at`,
  `orig_msg_id` = our `notify_when_idle` msg_id, delivered over a fresh reverse
  connection. The optional `detail` (one-line turn summary) was ABSENT even
  though we were registered — its gating is stricter than "requester is
  registered" or is permission-mode dependent. UNKNOWN.
- Both our sends got `sync reply bytes=0` — no synchronous ack, all feedback
  async. [OBS]

**NEW FIELD — `hop-chain` [OBS], undocumented, high impact:**
The ack's wrapper carried an attribute absent from both our send AND the
original human-initiated `c2c-d5` send:
```
<cross-session-message from="uds:/tmp/cc-socks/25792.sock"
   hop-chain="82a5cf6f862d55ad646615c9" from-name="trois-bocaux-50" from-mode="prompting">
```
- 24 hex chars (12 bytes).
- Appeared ONLY on a message sent in RESPONSE to a received cross-session
  message — i.e. it tracks chained agent-to-agent hops. This is loop /
  provenance control for exactly the pattern a relay creates.
- **Bridge implication (critical):** a relay is a hop chain by construction
  (A → bridge → B → bridge → A). A bridge that strips `hop-chain` risks
  infinite ping-pong with no loop detection; one that mishandles it risks
  legitimate replies being dropped as loops. The bridge must participate in
  this mechanism, and its rules are not yet known.

### Follow-up experiments this capture opens

1. **`hop-chain` semantics — now the top priority.** Does it accumulate across
   hops (chain grows) or stay constant for a conversation? What triggers a
   loop-drop? Two ways to find out: grep the binary for `hop-chain`/`hopChain`,
   or drive a multi-hop ping-pong with two tap peers relaying between two real
   sessions and watch the value evolve.
2. **Hold path under stricter permission modes.** Repeat the send against a
   session NOT in auto mode; watch for `peer_message_status status:"held"` and
   whether the recipient user is prompted.
3. **`detail` gating on `peer_idle_notice`.** Determine why the turn summary
   was withheld despite registration.
4. **Inbound peer-pid check (still open).** Unchanged — needs a peer whose
   listener pid ≠ its registry pid, which fights the stale-entry reaper.

---

## hop-chain — resolved (extraction #2 + empirical check, 2026-09-23)

Source: `hopchain.md` (Gemini, non-round offsets, internally consistent, much
higher quality than the first extraction). One linchpin claim empirically
checked here; the rest accepted as [EXT] pending a multi-hop live test.

### Token derivation — deterministic alternative REFUTED [OBS-negative]

Gemini: a hop token = `HMAC-SHA256(key=kLb, msg=ownAddress)[:24]`, where `kLb`
is a 32-byte secret generated per process, in memory only.

Tested the one ground-truth pair (`uds:/tmp/cc-socks/25792.sock` →
`82a5cf6f862d55ad646615c9`) against eight keyless hashes (sha256/sha1/md5/
blake2s of the address, socket path, pid, various slices). **None matched.**
This refutes any deterministic, reproducible derivation and is consistent with
a per-process secret key. Not proof of HMAC specifically, but the design
consequence is settled:

**Tokens are opaque and non-forgeable. Only the issuing process recognizes its
own. A relay treats every token as an opaque 24-hex blob.**

### Accumulation & limits [EXT]

- Chain GROWS per hop: a replying session appends its own token
  (`F6o([...inbound], ownToken)`), capped at the last 32.
- `hop-runaway`: chain length > **28** → inbound silently dropped.
- `hop-loop`: receiver's own token appears ≥ **10** times in the inbound chain
  → silently dropped. (Both are same-uid, warn-logged, no error frame back.)
- Origination: first human-initiated send has no peer-origin turn behind it, so
  no hop-chain is emitted. An agent replying to a peer message carries it.
- Also present: a token-bucket rate limiter (cap 30, refill 0.5/s) and a 30 s
  dedup window on `(sender, body)`.

### The practical ceiling this imposes — matters for c2c

`hop-loop` at 10 self-appearances trips first. A sustained A↔B exchange
appends one A and one B token per round-trip, so **a back-and-forth dies at
~9–10 round-trips**, silently. Fine for a review (request → review → fix →
re-review). A ceiling for long iterative dialog. The only honest reset is the
sending agent starting a NEW message not framed as a reply (no peer-origin
behind it → fresh chain) — a decision that belongs to the agent, not the relay.

### Relay rules — corrected

1. **Forward the `<cross-session-message …>` wrapper's `hop-chain` verbatim.**
   The inbound parser (`pSd`) rejects any wrapper that doesn't round-trip
   byte-for-byte through its own reconstruction, so formatting must be exact.
2. **Rewrite `from` to a local bridge socket** so the far side's reply routes
   back to the bridge — and publish a correctly-hashed key file for that
   from-address (no-realpath) so the far side can auth its reply. Preserve
   `hop-chain` unchanged while doing so; build a fresh, correctly-formatted
   wrapper.
3. **Never append a bridge token and never trim the chain.** Appending burns
   the budget and pollutes self-detection; trimming self-tokens DISABLES the
   loop guard that prevents infinite cross-host ping-pong. (This directly
   contradicts `hopchain.md` §6 rule 3 — that advice is unsafe.)
4. Mind the rate limiter: a relay replaying a backlog faster than ~1 msg / 2 s
   per sender, or re-sending an identical body within 30 s, will be dropped.

### Protocol status: closed enough to build

Framing, auth, reverse channel, hold-in-auto-mode, and hop-chain are all
resolved. Remaining unknowns (hold under strict permission modes, `detail`
gating, inbound peer-pid check) don't block a bridge targeting auto-mode
sessions. Next artifact: bridge architecture.

Closed since, negatively: `peer_message_status` carries nothing to a human --
see "peer_message_status is invisible to the sender" below. Sender-facing
feedback has to be an injected `user` message.

---

## Registry identity is per-process — confirmed 2026-09-23

A probe published two peer identities from ONE process: same alive pid in both
registry entries, distinct sockets and key files, but NON-pid registry
filenames (`c2cmid-alpha.json`, `c2cmid-beta.json`). Neither appeared in
`ListAgents`, and the probe's sockets were never connected to (ListAgents'
250ms probe never fired against them). **[OBS]**

Conclusion: the registry reader keys on the filename `<pid>.json` — **exactly
one peer identity per process.** To expose N named peers you need N processes.

Ferry consequence: for 2 hosts, the ferry is a single process = a single peer
representing the other host (directed == fan-out when there's only one other
host). For 3+ hosts, a supervisor + one child process per exposed peer. v1
targets the 2-host single-peer model.

---

## `peer_message_status` is invisible to the sender — 2026-09-25

Asked because c2c wants to tell a sending session "held: nothing on the far
host is running that project" instead of dropping it to a log line nobody
reads. `peer_message_status` looked like the built-in answer. It is not.
Read out of the 2.1.236 bundle; no frame was sent. **[SRC]**

### The receive path ends in a no-op

Dispatch for the frame (`O8m`) finishes with:

```js
else { if (e.status==="held") Ola(i); else if (e.status==="delivered" && o?.wasHeld) Dla(i);
       qS().onPeerMessageStatus?.(e.status, i) }
```

- **`Ola`/`Dla` are the outbound rate limiter, not UI.**
  `Ola(e){O5d(e,(t,r)=>t.credit(r))}`, `Dla` debits, both against
  `Qh().outbound.pacer` for that target. A `held` status **credits the
  sender's burst budget back** (the message hasn't consumed the recipient's
  capacity yet); a `delivered` that follows a `held` debits it again.
- **`onPeerMessageStatus` is `null` by default** (`x8m` field initializer) and
  exactly one site in the whole bundle registers it — the headless path, which
  writes one debug line: ``T(`[headless] cross-session hold-receipt:
  status=${so} from=${...}`)``. No interactive/TUI registration exists, so in
  an interactive session the optional call is a no-op.
- **`reason` is never read by the receiver.** `Mla` returns only
  `{destination, wasHeld}`; the call site forwards `e.status` and that
  destination. The canned `reason` strings (`GzT`) are composed by the
  *sender* and discarded on arrival.

So a status frame cannot carry a message to a human, and a fabricated one
silently inflates the recipient's rate-limit budget. Don't emit these to
convey information.

### Frame shape and the gates it must pass

Recorded because it took real work to pin down, and the emitter is the model
for anything c2c sends:

```js
// emitted by the hold-receipt hook when an inbound message is held
eNr(replyTarget, {action:"peer_message_status", status, reason:GzT(status),
                  from: ownSockUri, orig_msg_id: origin.msg_id},
    {expectPeerPid: origin.verifiedPeerPid})
// -> N5d(target, {type:"control", action:..., ..., msgV, msg_id})
```

Four gates, any of which discards it:

1. `status` ∉ `held|denied|expired|delivered` → no branch matches, no log.
2. `session_id` present and ≠ recipient's → dropped by `$8m`.
3. `orig_msg_id` must match a send the **recipient** is still tracking:
   `Mla` searches `receipts.outstandingSends`, then `awaitingTerminal` for a
   non-`held` status. A miss logs `peer_message_status dropped: no outstanding
   send matches orig_msg_id=…`. Sends are only tracked when `trackReceipts` is
   on (default), via `M5d(msg_id, target)` in `CDn`.
4. The reply address must satisfy `Eqi`: starts with `uds:` and resolves inside
   the recipient's own socket namespace (`uZs`). The ferry's `/tmp/cc-socks`
   default passes.

Gate 3 is why a cold probe with a made-up `orig_msg_id` would have produced a
null result indistinguishable from "nothing is listening".

### Where "held" *is* visible: the receiving side

The hold UI belongs to the person being messaged, driven by the
`crossSessionInbound` setting, with real user-facing strings per cause:
`explicit-setting`, `managed-setting` (managed policy beats a local
`accept`), `repo-setting` (a repo may only tighten), `mode-unknown`,
`mode-mismatch`, `no-mode-asserted`. Nothing in that surface faces the
sender. **[SRC]**

### Consequences for c2c

- The only channel that reaches a sending session's transcript is a plain
  `user` message injected into it — what the ferry already does. Any
  "held / undeliverable" feedback has to ride that.
- Such a self-generated note must **not** be recorded in the ferry's
  pending-reply correlation, or the session's next message is misclassified as
  a reply to a note the ferry invented.
- Status notes spend the same per-target budget as real traffic
  (`jLb().reserve(...)`, plus the ~1 msg/2 s and identical-body-within-30 s
  limits in "Relay rules"). One note per dropped message can get itself
  dropped — they need coalescing, not one per event.

### Incidental findings

- `CLAUDE_CODE_MESSAGING_TOKEN` carries the **childToken**, not the
  `peerToken`. The bundle also logs a ready-made inject one-liner:
  `{ echo '{"type":"auth","token":"'"$CLAUDE_CODE_MESSAGING_TOKEN"'"}';
  echo '{"type":"user","message":{"role":"user","content":"hello"}}'; } |
  socat - UNIX-CONNECT:$SOCKET`. **[SRC]**
- Auth can be optional on some platforms: a failure to publish the inbox key
  is fatal when `authRequired`, and a warning otherwise
  ("peers will send unauthenticated"). c2c should keep asserting auth. **[SRC]**
