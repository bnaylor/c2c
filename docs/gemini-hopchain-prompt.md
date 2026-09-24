Extract how Claude Code's cross-session messaging implements the `hop-chain`
mechanism. This is a focused follow-up to an earlier extraction (you produced
peer-protocol-v1.md). I'm building a relay that bridges these messages between
two hosts, and a relay is a hop chain by construction, so I need this exact.

## Target

/opt/homebrew/Caskroom/claude-code/2.1.236/claude
  - 317 MB Mach-O 64-bit arm64, JS-runtime single-file executable, minified.
  - DO NOT read it into context; it will not fit. Use `strings -t d` for
    byte offsets, `dd` to carve windows, beautify, then read the code.

## What I already know (anchors, do not re-derive)

Cross-session messages carry a `user` payload whose `message.content` wraps the
text in an XML-like tag. Observed on the wire in two forms:

  Original human-initiated send (NO hop-chain):
    <cross-session-message from="uds:/tmp/cc-socks/25002.sock"
       from-name="c2c-d5" from-mode="prompting"> ... </cross-session-message>

  An agent's REPLY to a received cross-session message (HAS hop-chain):
    <cross-session-message from="uds:/tmp/cc-socks/25792.sock"
       hop-chain="82a5cf6f862d55ad646615c9" from-name="trois-bocaux-50"
       from-mode="prompting"> ... </cross-session-message>

So `hop-chain` appears only when a message is sent as part of a chain (a reply
to / continuation of a previously received cross-session message). The value
above is 24 hex chars (12 bytes). The wrapper is built by a function near
offset 279944973 (identifier `NMr` in the earlier extraction); the escaping
helper was `OMr`. Start there and follow the data flow of the hop-chain value.

## The questions I need answered, in priority order

1. FORMAT. What is the 24-hex value? A single 12-byte id? Two 6-byte ids
   concatenated? A hash? Where is it generated (which randomBytes/hash call)?

2. ACCUMULATION. This is the crux. When a session receives a message with
   hop-chain = X and then sends a reply, is the outgoing hop-chain:
     (a) the same X carried through unchanged (conversation-constant), or
     (b) X with a new hop id appended (chain grows each hop), or
     (c) a fresh value derived from X?
   Find the code that reads an inbound hop-chain and the code that writes an
   outbound one, and show how the outbound value is computed from the inbound.

3. LOOP DETECTION / DROP. Is there code that inspects hop-chain to detect a
   cycle or enforce a max hop count and DROP or refuse a message? If so: what's
   the exact condition (self-id already present in chain? length > N?), what's
   the limit N, and what happens on trip (silent drop, logged warning, error
   frame)? Quote it.

4. ORIGINATION. Why does a first human-initiated send have NO hop-chain but an
   agent reply does? Find the branch that decides whether to emit the field at
   all. Is it "emit only when replying within an existing chain", or
   "emit whenever the sender is an agent turn vs a human prompt"?

5. IDENTITY. What identifies a "hop"? Is a hop id tied to a session
   (sessionId), a socket path, a process, or the message? This determines
   whether a relay that re-emits on a different socket looks like the same hop
   or a new one.

## Why each answer changes my relay design

- If chain GROWS per hop (2b): my relay must forward hop-chain intact (or
  append correctly) or cross-host replies get dropped as over-limit loops.
- If loop detection keys on self-id-in-chain: my relay re-emitting a message
  toward the far host must NOT inject an id that the far session will read as
  its own, or legitimate messages die.
- If there's a max hop count: two bridged agents doing review ping-pong will
  hit it; I need to know N to decide whether the relay resets or preserves it.

## Deliverable

Write /Users/bnaylor/src/c2c/docs/hopchain.md with:

- The generation site (offset + beautified snippet).
- The inbound-read and outbound-write sites (offsets + snippets), and the exact
  transform between them (answering Q2).
- The loop/limit check if any (offset + snippet + the condition and N).
- The origination branch (offset + snippet, answering Q4).
- A plain-English statement of the rule a correct relay must follow to
  participate without breaking loop detection.

## Rigour

- Tag every claim OBSERVED (you read the code; cite offset + snippet) or
  INFERRED (reasoning from names/structure). I will only implement against
  OBSERVED claims.
- Round-number byte offsets are a tell you guessed; real offsets are not round.
  If you cannot find the actual code for a claim, say so — do not invent it.
- Two prior extraction errors to avoid repeating: the key-file hash uses the
  socket path AS WRITTEN, not realpath-canonicalized; and the `from` field is
  plain, not url-encoded. Both were fabricated as OBSERVED last time. Verify,
  don't assume.
- If hop-chain turns out NOT to drive loop detection at all (e.g. it's purely
  informational provenance), say that explicitly — a negative result is a
  valid, useful answer here.
