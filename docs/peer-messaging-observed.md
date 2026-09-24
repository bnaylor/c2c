# Claude Code local peer messaging — observed mechanism

Findings from inspecting a live install (claude 2.1.236, macOS arm64) on
2026-09-23. Scope: how sessions on one host discover and message each other,
and what that implies for a cross-host bridge.

Claims are tagged **OBSERVED** (read off the filesystem or a tool result) or
**INFERRED** (reasoning from structure, not directly seen). Nothing here is a
public contract; treat all of it as version-fragile.

Companion file: `gemini-frame-extraction-prompt.md` drives the wire-format
extraction that this document deliberately stops short of. That work writes
`peer-protocol-v1.md`.

## Summary

Three layers, all local-filesystem:

| Layer | Mechanism |
|---|---|
| Discovery | `~/.claude/sessions/<pid>.json`, one per live session |
| Auth | `~/.claude/sessions/<pid>.<sha256>.key`, mode 0600 |
| Transport | `/tmp/cc-socks/<pid>.sock`, unix domain socket, mode 0600 |

There is no mailbox, no queue directory, and no hook involvement. Delivery is
process-to-process over the socket.

## Discovery registry — OBSERVED

Each live session writes `~/.claude/sessions/<pid>.json`:

```json
{"pid":18506,"sessionId":"69ab1147-418e-415c-b8ce-5f6d2ae0319b",
 "cwd":"/Users/bnaylor/src/c2c/docs","startedAt":1790209842330,
 "procStart":"Thu Sep 24 00:30:39 2026","version":"2.1.236",
 "peerProtocol":1,"peerFeatures":["notify_idle"],
 "kind":"interactive","entrypoint":"cli",
 "messagingSocketPath":"/tmp/cc-socks/18506.sock",
 "name":"docs-eb","nameSource":"derived","nameSince":1790209842330,
 "status":"busy","updatedAt":1790210291045,"statusUpdatedAt":1790210291045}
```

Field notes:

- `kind` observed as `interactive` and `bg`.
- `status` observed as `busy` and `idle`, with its own `statusUpdatedAt`.
- `name` is the address used by `SendMessage`; `nameSource:"derived"` on every
  session seen, all derived from the `cwd` basename plus a short suffix
  (`docs-eb` for `/Users/bnaylor/src/c2c/docs`).
- `procStart` is a ctime string, presumably to detect pid reuse — INFERRED.
- `peerProtocol:1` and `peerFeatures:["notify_idle"]` are identical across all
  five sessions observed.

`ListAgents` output matched the registry exactly (names, `kind`, `status`,
start times) and omitted the calling session. Whether `ListAgents` reads the
registry directly or queries peers over their sockets is UNDETERMINED.

## Auth key — OBSERVED

Sibling file, mode 0600:

```json
{"peerToken":"9d3b3237459f372ba9e495bfd2b80a94","procStart":"Thu Sep 24 00:30:39 2026"}
```

`peerToken` is 32 hex chars (128 bits).

**The filename's hash component is `sha256(messagingSocketPath)`.** Verified
against all five live sessions:

```
sha256("/tmp/cc-socks/18506.sock")
  = 4391eeaf702d16ef52cfde4dc0c883af45078927ce75778c9628f540b016ffbf
file = 18506.4391eeaf…ffbf.key
```

Ruled out as the hashed input: `sessionId`, `pid`, `cwd`, `procStart`, `name`,
`peerToken`, and several concatenations of those.

Consequence: given a registry entry, a peer can derive the exact key filename
with no directory scan. That the token is then presented to the recipient over
the socket to authenticate is INFERRED — not yet seen on the wire.

Security model is plain unix perms: the key files are 0600, the sockets are
0600, and `/tmp/cc-socks/` is 0700. Everything is same-uid.

## Transport — OBSERVED

`/tmp/cc-socks/` contained exactly one `srw-------` socket per live registry
entry, named `<pid>.sock`, matching each entry's `messagingSocketPath`.

No queue, spool, or inbox files exist anywhere under `~/.claude`. A search for
`*msg*`, `*mail*`, `*inbox*`, `*ipc*`, `*peer*` directories returned only
unrelated project paths.

## Delivery path — INFERRED

The `Message from @<name> (ctrl-o to expand)` line in a receiving session is
the rendering of a live socket delivery, injected into that process's event
loop and picked up by the agent at its next turn boundary.

Supporting evidence: a socket per session, no persisted queue, no hook
configured to poll one (`~/.claude/hooks/` is empty; the only hook in
`settings.json` is an unrelated `PreToolUse` command). Not yet confirmed by
observing a frame.

`SendMessage` calls appear in transcripts with `to`, `summary`, and `message`
— OBSERVED. No inbound peer message was found persisted in any transcript
under the patterns searched, which suggests inbound delivery is rendered as a
UI event rather than written into the `.jsonl` the way user turns are —
INFERRED, and worth re-checking, since it affects whether a bridge can audit
what was delivered.

`notify_idle` presumably lets a sender defer delivery until the recipient's
`status` flips to `idle` — INFERRED from the feature name plus the presence of
`status`/`statusUpdatedAt` in the registry.

## Background worker daemon — OBSERVED, adjacent

`~/.claude/daemon/roster.json` tracks daemon-spawned workers with their own
sockets:

```json
{"proto":1,"supervisorPid":45331,"workers":{"e916589f":{
  "pid":45339,"sessionId":"…","rendezvousSock":"/tmp/cc-daemon-501/<id>/rv/<short>.sock",
  "ptySock":"/tmp/cc-daemon-501/<id>/spare/<id>.pty.sock","cwd":"…","dispatch":{…}}}}
```

Same architectural pattern (registry file + unix sockets), different purpose:
this is session spawning and PTY attach, not peer messaging. `daemon.log`
showed only auth-refresh activity. Probably not needed for the bridge, but
it's the precedent for how an out-of-process component registers itself.

## Implications for c2c

1. **Account and hosting are irrelevant to this channel.** It never touches
   Anthropic auth. Anthropic-hosted on one laptop and Vertex AI on the other
   makes no difference to peer messaging.

2. **The bridge should pose as a local session on each host.** Write a
   registry entry, open a socket, relay to the peer host. Then discovery,
   addressing, and UX all come free: the bridge appears in `ListAgents`, and
   any session reaches it with `SendMessage({to: "work", …})`. No plugin, no
   skill, no MCP server.

3. **Both protocol halves are required.** Posing as a peer means serving the
   socket (to receive from local sessions) *and* connecting to peer sockets
   (to deliver inbound). No way around reversing the frame format.

4. **Namespace the names.** `name` derives from `cwd` basename, so the same
   checkout produces the same name on both laptops — `iris-ac` will collide.
   Prefix at the bridge (`work/iris-ac`).

5. **Fail closed on version drift.** Gate on `version` and `peerProtocol` from
   the registry; refuse to speak rather than send malformed frames after an
   upgrade.

6. **Design for absent recipients.** Local peer messaging assumes the target
   process is live. Across hosts that's usually false, so the relay needs
   durable store-and-forward that local peering doesn't have — probably
   replaying to a session in the matching repo when one appears, keyed on the
   GitHub URL.

## What is not known

- The frame format: framing, handshake, auth exchange, message types, acks,
  error shapes. This is `peer-protocol-v1.md`'s job.
- Whether `ListAgents` uses the socket at all, or only reads the registry.
- Registry lifecycle: update cadence, who reaps stale entries, how `status`
  transitions are driven.
- Whether inbound peer messages are persisted anywhere.

An attempt to pull protocol strings from the `claude` binary
(`/opt/homebrew/Caskroom/claude-code/2.1.236/claude`, 317 MB Mach-O arm64) was
blocked by the local permission classifier, so no binary inspection informed
this document.

## Next experiment: socket tap

Cheaper and higher-fidelity than binary analysis, and it yields ground truth
to check the extracted spec against:

1. Start a throwaway session; note its `messagingSocketPath`.
2. Rewrite that session's registry JSON to point `messagingSocketPath` at a
   proxy socket path.
3. Run a proxy that logs frames and forwards to the real socket.
4. `SendMessage` to it from another session.

Senders read the path out of the registry, so they connect through the tap.
Note the key filename is `sha256(messagingSocketPath)` — after rewriting the
path, a sender deriving the key filename from the *new* path will not find a
key file, so the proxy likely needs a matching key file at the derived name.
