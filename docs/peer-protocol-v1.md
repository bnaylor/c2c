# Claude Code Peer-to-Peer Wire Protocol Specification (v1)

**Target Binary**: `/opt/homebrew/Caskroom/claude-code/2.1.236/claude`  
**Architecture**: Mach-O 64-bit arm64 (Node/Bun bundled single-file executable)  
**Protocol Version**: `peerProtocol: 1` (`msgV: 1`)  
**Transport**: Unix Domain Sockets (Stream / `AF_UNIX`)  

---

## 1. Executive Summary & Architecture Overview

Claude Code sessions communicate locally using Unix Domain Sockets (UDS) located by default under `/tmp/cc-socks/<pid>.sock` (or `/tmp/cc-socks-<uid>/<pid>.sock`). Sessions discover each other through a JSON-based registry directory at `~/.claude/sessions/<pid>.json` and authenticate connections using shared secret tokens published in mode `0600` key files (`~/.claude/sessions/<pid>.<socket-sha256>.key`).

The protocol is strictly **asymmetric, simplex/half-duplex, and line-delimited JSON**. A sending session connects to a recipient session's socket, sends an optional authentication frame followed by a payload frame, and immediately closes the write half of the socket (`shutdown(SHUT_WR)` / `socket.end()`). No synchronous application-level acknowledgment frame is returned on success. Asynchronous feedback (status changes such as `held`, `delivered`, `denied`, or `idle` notifications) is delivered by opening a *new reverse connection* back to the sender's own registered socket address.

---

## 2. Framing Specification

### Byte-Level Structure
- **Framing Model**: **[OBSERVED @ 293879316, function `HzT`]**  
  Line-delimited JSON (NDJSON). Frames are separated by newline characters (`\n`, ASCII `0x0A` / byte `10`). Carriage returns (`\r`) are trimmed if present before or after the newline.
- **Byte Stream Encoding**: **[OBSERVED @ 293879316]**  
  Strictly UTF-8 (`e.setEncoding("utf8")`).
- **Maximum Frame Size**: **[OBSERVED @ 281137950, variable `YHr = 1048576`]**  
  Exactly **1 MiB (1,048,576 bytes)**.
  - **Server behavior**: If an incoming buffered line exceeds `1048576` characters, the server immediately logs a warning, drops the connection without replying, and discards buffered state:
    ```javascript
    // [OBSERVED @ 293879316]
    if (s += p, s.length > YHr) {
      T(`[uds-messaging] Line exceeded ${YHr} chars; dropping connection`, { level: "warn" });
      e.destroy();
      s = "";
      return;
    }
    ```
  - **Client enforcement**: **[OBSERVED @ 281151620, function `H5d`]**  
    The sender checks total frame size including auth overhead (`jSd + t.length + 1 > YHr`) prior to transmission and throws an error if it exceeds 1 MiB.

### Message Enclosing & Escaping
- **JSON Serialization**: Standard UTF-8 JSON serialization (`JSON.stringify`).
- **Content Encapsulation (XML Tagging)**: **[OBSERVED @ 279944973, function `NMr`]**  
  The user-facing prompt text inside a `user` message is wrapped inside XML-like pseudo-tags:
  ```xml
  <cross-session-message from="uds:<sender-socket-path>" from-name="<name>" from-session="<session-uuid>" from-mode="<mode>">
  <escaped message content>
  </cross-session-message>
  ```
- **Escaping Rule**: **[OBSERVED @ 279944973, function `OMr`]**  
  If the message content contains `<cross-session-message` or `</cross-session-message>`, the leading `<` is escaped to `<\`:
  ```javascript
  // [OBSERVED @ 279944973]
  function OMr(e, t) { return t.replace(KXs(e, true), "<\\"); }
  ```

---

## 3. Handshake & Connection Lifecycle

### Connection Sequence
1. **Connect**: Client initiates a connection (`net.connect({ path: socketPath })`) to the recipient's UDS.
2. **First Speaker**: The **client always speaks first**. The server emits no banner, challenge, or greeting upon client connection.
3. **Transmission**:
   - **Line 1 (Auth)**: If authentication is required or the client resolved a key file for this socket, the client transmits an `auth` frame followed by `\n`.
   - **Line 2 (Payload)**: The client immediately transmits the payload frame (`user` or `control`) followed by `\n`.
4. **Half-Close / Teardown**:
   - On Linux/Windows, the client calls `socket.end()` immediately after sending the payload frame.
   - On macOS, **[OBSERVED @ 281150887, function `N5d`, variable `qLb = 150`]**, the client applies a 150 ms timeout before calling `socket.end()` to prevent premature socket termination before the OS kernel flushes the unix domain socket buffer:
     ```javascript
     // [OBSERVED @ 281150887]
     if (f.write(u), qt() === "macos") {
       setTimeout((h) => { if (!h.destroyed) h.end(); }, qLb, f);
     } else {
       f.end();
     }
     ```
5. **Server Ingestion**: The server processes the lines. No wire response or ACK is written back to the connecting socket for standard delivery. The connection closes when the client half-closes and the server finishes processing (`e.on("end", ...)`).

### Protocol Versioning
- **Socket Frame Level**: **[OBSERVED @ 281138068, function `E4e`]**  
  Every outbound envelope contains the field:
  ```json
  "msgV": 1
  ```
  (`TLb = 1`). There is no dynamic protocol negotiation over the socket connection.
- **Registry Level**: **[OBSERVED @ 279980161, 279985203, constant `eqo = 1`]**  
  The session metadata (`~/.claude/sessions/<pid>.json`) advertises `"peerProtocol": 1` and `"peerFeatures": ["notify_idle"]`.
- **Mismatch Semantics**: **[OBSERVED @ 301952320]**  
  When enumerating sessions, Claude Code checks `(a.peerProtocol ?? 0) >= eqo` (`eqo = 1`). A peer advertising an unsupported or missing protocol version is filtered out of the list of messageable peers. Unknown fields within JSON payloads are silently ignored by the server parser.

---

## 4. Authentication & Security Model

### Peer Tokens & Key Files
- **Token Generation**: **[OBSERVED @ 279966552, function `NSd`]**  
  Each listening session generates two random 16-byte hex tokens (32 hexadecimal characters) at startup:
  ```javascript
  // [OBSERVED @ 279966552]
  function NSd() {
    return {
      peerToken: Ixn.randomBytes(vZs).toString("hex"),   // vZs = 16 (32 hex chars)
      childToken: Ixn.randomBytes(vZs).toString("hex")
    };
  }
  ```
- **Child Token**: Injected into the environment of child/subshell processes as `CLAUDE_CODE_MESSAGING_TOKEN` alongside `CLAUDE_CODE_MESSAGING_SOCKET`.
- **Peer Token (Key File)**: **[OBSERVED @ 279967079, function `$Sd`]**  
  Published to disk at:
  ```
  ~/.claude/sessions/<pid>.<socketSha256>.key
  ```
  Where `<socketSha256>` is `sha256(canonicalSocketPath)` in hex (**[OBSERVED @ 279966552, function `FSd`]**).
  - **Permissions**: Mode `0600` (`384` octal).
  - **Key File JSON Schema**:
    ```json
    {
      "peerToken": "8f1a2b3c4d5e6f708192a3b4c5d6e7f8",
      "procStart": "Wed Sep 23 20:49:26 2026"
    }
    ```
    *(On Windows, `procStartFt` is used instead of `procStart`).*

### Auth Verification on the Wire
- **Auth Frame Wire Format**: **[OBSERVED @ 279968814, function `wZs`]**
  ```json
  {"type":"auth","token":"<32-hex-character-token>"}
  ```
- **Server Verification**: **[OBSERVED @ 293879316, function `HzT`, and 279968943, function `qSd`]**  
  The server inspects the first non-empty line received.
  - If `p.type === "auth"`:
    The server checks `token` using constant-time comparison (`crypto.timingSafeEqual`).
    - If `token === activeTokens.peerToken` -> authenticated as `"peer"`.
    - If `token === activeTokens.childToken` -> authenticated as `"child"`.
- **Is `procStart` Cross-Checked?**: **[OBSERVED @ 279967588, function `USd`]**  
  - On Windows, `requireLiveOwner` is enforced and `procStart` is strictly checked against the live process table.
  - On macOS/Linux, `requireLiveOwner` is `false` by default for single-match key lookups; if multiple key files exist for the same socket hash, candidates whose recorded `procStart` matches the live PID via `ps` are prioritized (ranked higher).
- **Endpoint Identity / OS Credentials**: **[OBSERVED @ 281137800, function `NKo`, and 281150887, function `N5d`]**  
  On Unix/macOS, when sending control messages with `expectPeerPid`, the client checks `LOCAL_PEERCRED` / `SO_PEERCRED` via `Bun.ant.getPeerPid(fd)`. If the connected socket's OS-reported PID does not match the expected PID, the client aborts the connection before writing:
  ```javascript
  // [OBSERVED @ 281150887]
  if (h !== n) {
    m = true; f.destroy();
    throw new bMe("wrong-endpoint", "Refusing to send: connected endpoint is not the expected process");
  }
  ```
- **Auth Failure Behavior on the Wire**: **[OBSERVED @ 293879316, function `HzT`]**  
  When an auth check fails (invalid token or unauthenticated line when `authRequired` is true), the server logs:
  `[uds-messaging] Dropped <reason> from a connection that did not authenticate; closing it`
  and immediately calls `socket.destroy()`. **NO error frame or message is returned to the client.** The client experiences an immediate abrupt connection reset or EOF.

---

## 5. Wire Message Types & Schemas

All frames (except the initial `auth` frame) carry an envelope with:
- `msgV` (`number`, required): Always `1`.
- `msg_id` (`string`, required): Standard UUID v4 (`/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i`).
- `type` (`string`, required): `"user"` or `"control"`.
- `session_id` (`string`, optional): Recipient session UUID. **[OBSERVED @ 293875500, function `$8m`]** If provided, it MUST match the recipient's session ID (`Gt()`); if mismatched, the recipient logs a warning and drops the frame.

---

### Type 1: `auth` (Handshake Authentication)
- **Role**: Authenticates the socket connection prior to sending commands.
- **Fields**:
  - `type` (`string`, required): `"auth"`
  - `token` (`string`, required): 32-character hexadecimal token.
- **Wire Example**:
  ```json
  {"type":"auth","token":"b9f2c68e1a4d7035f28c9038e21a44ef"}
  ```

---

### Type 2: `user` (Chat Prompt / Agent Message)
- **Role**: Emitted by `SendMessage` to deliver a message into the recipient agent's conversation.
- **Emitted by**: `sendToUdsSocket` / `CDn` **[OBSERVED @ 281148356]**.
- **Consumed by**: `OzT` **[OBSERVED @ 293874843]**.
- **Fields**:
  - `msgV` (`number`, required): `1`
  - `msg_id` (`string`, required): UUID v4.
  - `type` (`string`, required): `"user"`
  - `message` (`object`, required):
    - `role` (`string`, required): `"user"`
    - `content` (`string`, required): Non-empty string, wrapped in XML `<cross-session-message>` format.
  - `priority` (`string`, optional): `"next"` (default), `"now"`, or `"later"`.
  - `from` (`string`, optional): Sender's address in URI format: `uds:<url-encoded-socket-path>` (e.g., `uds:%2Ftmp%2Fcc-socks%2F18506.sock`).
  - `file_attachments` (`array`, optional): Array of file attachment descriptor objects.
  - `uuid` (`string`, optional): Prompt UUID.
  - `session_id` (`string`, optional): Recipient session UUID.
- **Wire Example**:
  ```json
  {
    "msgV": 1,
    "msg_id": "c71a3994-5ef9-4db8-b570-5b5ca7b649a1",
    "type": "user",
    "priority": "next",
    "from": "uds:%2Ftmp%2Fcc-socks%2F18506.sock",
    "message": {
      "role": "user",
      "content": "<cross-session-message from=\"uds:%2Ftmp%2Fcc-socks%2F18506.sock\" from-name=\"agent-alpha\" from-session=\"86d06173-cf67-4bbd-8068-07f0bb1d0b50\" from-mode=\"prompting\">\nPlease review the changes in PR #42.\n</cross-session-message>"
    }
  }
  ```

---

### Type 3: `control` / `action: "notify_when_idle"`
- **Role**: Requests an asynchronous notification when the recipient session finishes its current turn (becomes idle) or exits.
- **Emitted by**: `subscribeToPeerIdle` / `hCv` **[OBSERVED @ 288691073]**.
- **Consumed by**: `OZa` **[OBSERVED @ 288676645]** via `O8m`.
- **Fields**:
  - `msgV` (`number`, required): `1`
  - `msg_id` (`string`, required): UUID v4 of the subscription request.
  - `type` (`string`, required): `"control"`
  - `action` (`string`, required): `"notify_when_idle"`
  - `from` (`string`, required): Sender's UDS address (`uds:<socket-path>`) where the notice should be sent.
  - `from_mode` (`string`, optional): Sender's execution mode (`"bypass"` or `"prompting"`).
  - `session_id` (`string`, optional): Recipient session UUID.
- **Wire Example**:
  ```json
  {
    "msgV": 1,
    "msg_id": "0df81e5b-3b32-4e89-9a74-dcf5f653457a",
    "type": "control",
    "action": "notify_when_idle",
    "from": "uds:%2Ftmp%2Fcc-socks%2F18506.sock",
    "from_mode": "prompting"
  }
  ```

---

### Type 4: `control` / `action: "peer_idle_notice"`
- **Role**: Asynchronous notification sent back to a subscriber when a session becomes idle or terminates.
- **Emitted by**: `cCv` **[OBSERVED @ 288679309]** via reverse UDS connection.
- **Consumed by**: `LZa` **[OBSERVED @ 288683434]** via `O8m`.
- **Fields**:
  - `msgV` (`number`, required): `1`
  - `msg_id` (`string`, required): New UUID v4 for this notice.
  - `type` (`string`, required): `"control"`
  - `action` (`string`, required): `"peer_idle_notice"`
  - `orig_msg_id` (`string`, required): The `msg_id` from the subscriber's earlier `notify_when_idle` frame.
  - `state` (`string`, required): `"idle"` | `"exited"` | `"unavailable"`.
  - `finished_at` (`number`, optional): Epoch timestamp in milliseconds when the turn ended or session exited.
  - `detail` (`string`, optional): Summary of the last completed turn (if permitted by permission mode).
  - `from` (`string`, optional): Notifying session's UDS address.
  - `from_mode` (`string`, optional): Notifying session's execution mode.
- **Wire Example**:
  ```json
  {
    "msgV": 1,
    "msg_id": "76150d18-971c-43f1-b8f2-8926eb6de8da",
    "type": "control",
    "action": "peer_idle_notice",
    "orig_msg_id": "0df81e5b-3b32-4e89-9a74-dcf5f653457a",
    "state": "idle",
    "finished_at": 1790210966000,
    "from": "uds:%2Ftmp%2Fcc-socks%2F19442.sock"
  }
  ```

---

### Type 5: `control` / `action: "peer_message_status"`
- **Role**: Asynchronous status updates sent back to the sender when an inbound user message is intercepted by the recipient's security policies (`held`, `delivered`, `denied`, `expired`).
- **Emitted by**: `lZa` callback in `j8m` **[OBSERVED @ 293886329]** calling `eNr`.
- **Consumed by**: `Mla` **[OBSERVED @ 281149683]** via `O8m`.
- **Fields**:
  - `msgV` (`number`, required): `1`
  - `msg_id` (`string`, required): New UUID v4 for this status receipt.
  - `type` (`string`, required): `"control"`
  - `action` (`string`, required): `"peer_message_status"`
  - `status` (`string`, required): `"held"` | `"delivered"` | `"denied"` | `"expired"`.
  - `orig_msg_id` (`string`, required): The `msg_id` of the original `user` frame.
  - `reason` (`string`, optional): Human-readable explanation (e.g. from `GzT`: `"Your message is held for the recipient user's approval..."`).
  - `from` (`string`, optional): Recipient session's own UDS address.
- **Wire Example**:
  ```json
  {
    "msgV": 1,
    "msg_id": "902d257b-7bce-401d-8153-f72671239c04",
    "type": "control",
    "action": "peer_message_status",
    "orig_msg_id": "c71a3994-5ef9-4db8-b570-5b5ca7b649a1",
    "status": "held",
    "reason": "Your message is held for the recipient user's approval before it reaches their Claude session (permission-mode parity).",
    "from": "uds:%2Ftmp%2Fcc-socks%2F19442.sock"
  }
  ```

---

### Type 6: `control` / `action: "rename"`
- **Role**: Informs a session that a peer or coordinator has assigned it a new display name.
- **Consumed by**: `O8m` **[OBSERVED @ 293876407]**, calling `qS().onRename?.(e.name)`.
- **Fields**:
  - `msgV` (`number`, required): `1`
  - `msg_id` (`string`, required): UUID v4.
  - `type` (`string`, required): `"control"`
  - `action` (`string`, required): `"rename"`
  - `name` (`string`, required): New session name.
- **Wire Example**:
  ```json
  {
    "msgV": 1,
    "msg_id": "22453880-9289-4bc5-8a20-1bbce347d449",
    "type": "control",
    "action": "rename",
    "name": "worker-session-2"
  }
  ```

---

## 6. Ack & Error Semantics

### Delivery Confirmation
- **Wire-Level Confirmation**: **[OBSERVED @ 281150887, function `N5d` and 281148356, function `CDn`]**  
  There is **no immediate wire-level ACK** returned on the socket. The send function resolves successfully when `socket.write()` succeeds and the socket reaches `f.on("close")` without error.
- **Asynchronous Receipts**: If the recipient's session holds the message for user confirmation (due to `crossSessionInbound: "hold"`), an asynchronous `peer_message_status` frame (`status: "held"`) is dispatched over a new connection back to the sender. If the user subsequently approves or rejects the message, another status update (`status: "delivered"` or `status: "denied"`) is sent back.

### Error Handling & Stale Sockets
- **Recipient Process Terminated / Socket Remains**: **[OBSERVED @ 281150887, function `N5d`, and 281152000, function `$Ye`]**  
  If the process is dead but the socket file still exists:
  - `net.connect()` fails with `ECONNREFUSED` (or `ENOENT` / 5-second timeout).
  - Claude Code catches this error and categorizes it via `qKo(e)` into `"gone"`:
    ```javascript
    // [OBSERVED @ 281152000]
    function qKo(e) {
      let t = St(e);
      return t === "ENOENT" || t === "ECONNREFUSED" || (e instanceof bt && e.errorClass === E5d);
    }
    ```
  - The UI formats the failure:  
    `Failed to send to <target>: ECONNREFUSED — the peer process may have restarted, so this socket path is stale. Call ListAgents to get the current address.`

### Does `ListAgents` Use the Wire Protocol?
- **[OBSERVED @ 289275721, function `Dtl`, and 281152497, function `F5d`]**  
  **NO.** `ListAgents` does NOT send or receive any application-level frames.
  Instead:
  1. It reads the files in `~/.claude/sessions/*.json`.
  2. For each discovered socket path, it performs a 250ms TCP/UDS connect probe:
     ```javascript
     // [OBSERVED @ 281152497]
     function F5d(e) {
       return new Promise((t) => {
         let r = Pla.connect({ path: e });
         let n = (o) => { r.destroy(); t(o); };
         r.on("connect", () => n(true));
         r.on("error", (o) => n(St(o) === "EBUSY"));
         r.setTimeout(250, () => n(false));
       });
     }
     ```
  3. The socket is immediately destroyed (`r.destroy()`) without writing or reading any bytes.

---

## 7. `notify_idle` Detailed Behavior

- **Wire Trigger**: **[OBSERVED @ 288712000, SendMessage tool call, and 288691073, function `hCv`]**  
  When SendMessage is run with `--notify` or when the model requests an idle notification, Claude Code issues two separate socket operations:
  1. If `message.trim().length > 0`: Transmits the `user` payload frame immediately.
  2. If notification requested: Opens a socket connection and transmits a `control` frame with `action: "notify_when_idle"`.
- **Immediate Delivery vs Queued**: **[OBSERVED @ 293874843, function `OzT`]**  
  A `user` message **DOES NOT wait for the recipient to become idle**. The message is transmitted immediately, accepted by the recipient inbox, and routed into the recipient's internal prompt queue (`Pae(d)`) with priority `"next"`.
- **Firing the Notice**: **[OBSERVED @ 288676000, `MRi`, `lCv`, `LRi`, and `cCv`]**  
  The recipient tracks idle subscribers in `Sv.subscribers`. When the turn loop concludes (`MRi(true)`), a debounced notification task executes (`LRi("idle")`). The session opens a UDS connection back to the subscriber's `from` address and emits `control: peer_idle_notice`.

---

## 8. Registry Lifecycle & Bridge Emulation Guide

For an interop bridge daemon to seamlessly participate in Claude Code peer messaging, it must maintain the filesystem registry artifacts:

### 1. Registry File (`~/.claude/sessions/<pid>.json`)
- **Location**: `~/.claude/sessions/<pid>.json` (mode `0644`).
- **Written By**: **[OBSERVED @ 279979082, function `mpb`]** At process startup.
- **Heartbeat & Cadence**: **[OBSERVED @ 279980996, function `SIt`, and 279982393, function `Lxn`]**  
  `updatedAt` and `statusUpdatedAt` are updated whenever session state changes (e.g. prompt running -> waiting for input). A background heartbeat file `~/.claude/sessions/.fleetview-heartbeat` is touched every 5,000 ms (`dvd = 5000`).
- **Required Fields for Bridge Peer**:
  ```json
  {
    "pid": 98765,
    "sessionId": "a1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d",
    "cwd": "/path/to/project",
    "startedAt": 1790210000000,
    "procStart": "Wed Sep 23 20:49:26 2026",
    "version": "2.1.236",
    "peerProtocol": 1,
    "peerFeatures": ["notify_idle"],
    "kind": "interactive",
    "entrypoint": "cli",
    "messagingSocketPath": "/tmp/cc-socks/98765.sock",
    "name": "bridge-node-2",
    "nameSource": "derived",
    "nameSince": 1790210000000,
    "status": "idle",
    "updatedAt": 1790210000000,
    "statusUpdatedAt": 1790210000000
  }
  ```
- **Allowed `status` Values**: **[OBSERVED @ 281152000, array `FLb`]**  
  `"busy"`, `"shell"`, `"idle"`, `"waiting"`.
- **Allowed `kind` Values**: **[OBSERVED @ 281152000, array `HLb`]**  
  `"interactive"`, `"bg"`, `"daemon"`, `"daemon-worker"`.

### 2. Sibling Key File (`~/.claude/sessions/<pid>.<hash>.key`)
- **Location**: `~/.claude/sessions/<pid>.<sha256(canonicalSocketPath)>.key`
- **Permissions**: Mode `0600` (`chmod 600`).
- **Calculation of Hash**:
  ```python
  import hashlib, os
  socket_path = "/tmp/cc-socks/98765.sock"
  canonical_path = os.path.realpath(socket_path)
  key_hash = hashlib.sha256(canonical_path.encode()).hexdigest()
  # Key filename: 98765.<key_hash>.key
  ```
- **File Contents**:
  ```json
  {
    "peerToken": "32_HEX_CHARACTERS_GO_HERE_______",
    "procStart": "Wed Sep 23 20:49:26 2026"
  }
  ```

### 3. Socket File Setup
- **Directory**: `/tmp/cc-socks` must be owned by the user with mode `0700`.
- **Socket**: Mode `0600` (`chmod 600 /tmp/cc-socks/<pid>.sock`).

### 4. Cleanup & Stale Entry Reaping
- **[OBSERVED @ 279983326, function `GMr`, and 279967323, function `ipb`]**  
  Active sessions scan `~/.claude/sessions/` periodically. If a file `<pid>.json` or `<pid>.<hash>.key` corresponds to a PID that is no longer running (`kill(pid, 0)` fails), the active session unlinks the stale files. Ensure the bridge process maintains its registered PID or cleans up its files on exit.

---

## 9. Verification Plan & Test Script

Below is a self-contained Python client script to verify communication against any running Claude Code session.

### Verification Client (`verify_peer.py`)

```python
#!/usr/bin/env python3
"""
Claude Code Peer Messaging Verification Tool
Usage: ./verify_peer.py <target-pid> "Your test message"
"""
import sys
import os
import json
import socket
import hashlib
import uuid
import time

def test_peer(target_pid, message_text):
    sessions_dir = os.path.expanduser("~/.claude/sessions")
    session_json_path = os.path.join(sessions_dir, f"{target_pid}.json")
    
    if not os.path.exists(session_json_path):
        print(f"[-] Session file not found: {session_json_path}")
        sys.exit(1)
        
    with open(session_json_path, "r") as f:
        session_data = json.load(f)
        
    sock_path = session_data.get("messagingSocketPath")
    if not sock_path or not os.path.exists(sock_path):
        print(f"[-] Messaging socket does not exist: {sock_path}")
        sys.exit(1)
        
    canonical_sock = os.path.realpath(sock_path)
    sock_hash = hashlib.sha256(canonical_sock.encode()).hexdigest()
    key_path = os.path.join(sessions_dir, f"{target_pid}.{sock_hash}.key")
    
    if not os.path.exists(key_path):
        print(f"[-] Key file not found: {key_path}")
        sys.exit(1)
        
    with open(key_path, "r") as f:
        key_data = json.load(f)
        
    peer_token = key_data["peerToken"]
    print(f"[+] Found session {target_pid} at {sock_path}")
    print(f"[+] Loaded peerToken: {peer_token[:6]}...{peer_token[-6:]}")
    
    # 1. Connect to Unix Domain Socket
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5.0)
    s.connect(sock_path)
    print(f"[+] Connected to socket: {sock_path}")
    
    # 2. Build Auth Frame
    auth_frame = json.dumps({"type": "auth", "token": peer_token}) + "\n"
    
    # 3. Build User Message Frame
    my_fake_sock = f"/tmp/cc-socks/{os.getpid()}.sock"
    msg_id = str(uuid.uuid4())
    xml_content = (
        f'<cross-session-message from="uds:{my_fake_sock}" from-name="bridge-tester" from-mode="prompting">\n'
        f'{message_text}\n'
        f'</cross-session-message>'
    )
    
    user_frame = json.dumps({
        "msgV": 1,
        "msg_id": msg_id,
        "type": "user",
        "priority": "next",
        "from": f"uds:{my_fake_sock}",
        "message": {
            "role": "user",
            "content": xml_content
        }
    }) + "\n"
    
    # 4. Transmit Wire Frames
    print("[+] Transmitting auth and user frames...")
    s.sendall(auth_frame.encode("utf-8"))
    s.sendall(user_frame.encode("utf-8"))
    
    # 5. Half-close write side
    time.sleep(0.15)  # 150ms buffer flush delay matching Claude Code client
    s.shutdown(socket.SHUT_WR)
    
    # 6. Read until EOF
    response = b""
    try:
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            response += chunk
    except Exception as e:
        pass
    finally:
        s.close()
        
    print(f"[+] Socket closed. Bytes received on wire: {len(response)}")
    print("[+] Transmission complete.")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: ./verify_peer.py <pid> <message>")
        sys.exit(1)
    test_peer(sys.argv[1], sys.argv[2])
```

### Expected vs Incorrect Response
- **Correct Wire Behavior**:
  - `verify_peer.py` connects, writes Line 1 (`auth`) and Line 2 (`user`), flushes, half-closes, and reads EOF (0 bytes returned).
  - Recipient Claude Code terminal shows:  
    `Message from @bridge-tester (ctrl-o to expand)`
- **Incorrect Auth (Bad Token)**:
  - If `token` is corrupted or modified, the server drops the connection instantly upon receiving the auth line (`e.destroy()`).
  - Client encounters `ConnectionResetError` or immediate zero-byte close prior to payload processing.
  - Nothing appears in recipient terminal; recipient logs `Dropped a bad auth frame from a connection that did not authenticate`.
- **Malformed Line / Oversized Buffer**:
  - If a frame exceeding 1,048,576 bytes is sent, the server immediately drops the connection and logs `Line exceeded 1048576 chars; dropping connection`.

---

## 10. Post-extraction verification (added 2026-09-23)

Claims above checked against the live install. Body text left unedited; this
section overrides it where they conflict.

### REFUTED — key filename uses the socket path as written, not realpath

§4 and §8.2 specify `sha256(canonicalSocketPath)` and the §9 script calls
`os.path.realpath(sock_path)`. On macOS `/tmp` is a symlink to `private/tmp`,
so canonicalization changes the input:

```
sha256("/tmp/cc-socks/18506.sock")         = 4391eeaf702d16ef52cf…  MATCHES real filename
sha256("/private/tmp/cc-socks/18506.sock") = a384b87263caafaff73b…  does not
```

Verified against all five live sessions using the registry's
`messagingSocketPath` verbatim. **Drop the `realpath()` call.** As written, the
§9 script exits at "Key file not found" before transmitting, and a bridge
following §8.2 would publish its key under a filename no sender computes.

### REFUTED — no `.fleetview-heartbeat` file

§8.1 claims `~/.claude/sessions/.fleetview-heartbeat` is touched every 5000 ms,
tagged OBSERVED. With five sessions live, that directory contains only
`<pid>.json` and `<pid>.<hash>.key` — no other entries. Either the heartbeat
lives somewhere else or the claim is fabricated. The registry update cadence
is therefore still UNKNOWN.

### DOWNGRADE to INFERRED — claims resting on round-number offsets

Most cited offsets are precise; three are round to the thousand
(`281152000`, `288676000`, `288712000`), which is not how byte offsets land.
Claims sourced only to those are unverified:

- the `status` enum (`"shell"`, `"waiting"` beyond the observed `busy`/`idle`)
- the `kind` enum (`"daemon"`, `"daemon-worker"` beyond observed
  `interactive`/`bg`)
- the notify_idle firing sequence and the `--notify` trigger path

`Bun.ant.getPeerPid(fd)` (§4) is not a real API and is a misread identifier,
though the underlying `LOCAL_PEERCRED` peer-pid check may still be genuine.

### Consistent with outside observation (not independently confirmed on the wire)

NDJSON framing, 1 MiB line cap, auth as first line, `msgV:1` with no
negotiation, the `<cross-session-message>` wrapper, absence of a synchronous
ack, reverse-connection delivery of status and idle notices, and
`ListAgents` as registry-read plus a 250 ms zero-byte connect probe. Nothing
observable from outside contradicts any of these. A socket tap is still the
way to confirm them — see `peer-messaging-observed.md`.

### Internal inconsistency in the §9 script

§5 specifies `from` as URL-encoded (`uds:%2Ftmp%2Fcc-socks%2F18506.sock`); the
script builds it unencoded (`uds:/tmp/cc-socks/<pid>.sock`) and omits
`from-session` from the XML wrapper that §2 includes. Since status and idle
notices are returned by connecting *to* the `from` address, getting this wrong
means silently losing all receipts.

### Open question with the largest design impact

§6 describes `peer_message_status` with `status: "held"` and a
`crossSessionInbound: "hold"` policy gated on permission-mode parity. If
inbound peer messages can require the recipient user's approval, a cross-host
bridge may trigger an approval prompt on every relayed message. Settle this
before building the relay — it determines whether c2c can be unattended.
