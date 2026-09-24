# Claude Code Cross-Session Messaging: Hop-Chain Extraction

This document details the exact mechanics of the `hop-chain` attribute used in Claude Code's cross-session peer messaging, based on binary analysis of `/opt/homebrew/Caskroom/claude-code/2.1.236/claude`.

---

## 1. Format & Token Generation

### Format
- **Wire Representation**: Comma-separated list of 24-character lowercase hexadecimal tokens:
  ```xml
  <cross-session-message from="uds:/tmp/cc-socks/25792.sock" hop-chain="82a5cf6f862d55ad646615c9" from-name="trois-bocaux-50" from-mode="prompting">
  ...
  </cross-session-message>
  ```
- **Validation Regex**: **[OBSERVED @ 279946659, 279946718]**
  ```javascript
  lSd = 24;
  LMr = 32;
  rSd = `[0-9a-f]{${lSd}}`;                          // 24 hex characters
  Mdb = `${rSd}(?:,${rSd}){0,${LMr - 1}}`;           // 1 to 32 comma-separated tokens
  uSd = new RegExp(`^${Mdb}$`);
  ```

### Generation Site
- **Key Generation**: **[OBSERVED @ 281139747, initializer `BKo`]**
  Each Claude Code process generates a private 32-byte cryptographically secure random key once at startup:
  ```javascript
  var b5d, kLb;
  var BKo = w(() => {
    SK();
    b5d = require("crypto");
    kLb = b5d.randomBytes(32);
  });
  ```
- **Token Derivation**: **[OBSERVED @ 279944365 (`cSd`), 281139659 (`mOt`)]**
  A hop token is the first 12 bytes (24 hex characters) of HMAC-SHA256 computed over the session's address URI (e.g. `"uds:/path/to/socket.sock"` or `"bridge:<sessionId>"`), keyed by the process-secret `kLb`:
  ```javascript
  // Offset 279944365
  function cSd(e, t) {
    return nSd.createHmac("sha256", t).update(e).digest("hex").slice(0, lSd); // lSd = 24
  }

  // Offset 281139659
  function mOt(e) {
    return cSd(e, kLb);
  }
  ```
- **Conclusion**: A single hop token is an opaque 24-hex-character string representing `HMAC-SHA256(key=kLb, msg=ownAddress).hex().slice(0, 24)`.

---

## 2. Inbound Read, Outbound Write, & Accumulation (Crux: Q2)

The chain **GROWS** on each hop (case 2b). Outgoing messages append a new token to the inbound chain.

### Inbound Read Site
1. **Wire Parsing**: **[OBSERVED @ 279945065, function `pSd`]**
   When an incoming XML message frame arrives, `pSd` extracts `hop-chain` and splits it on commas:
   ```javascript
   function pSd(e) {
     if (typeof e !== "string") return;
     let t = aSd.source.replace(/^\^|\$$/g, ""),
         r = uSd.source.replace(/^\^|\$$/g, ""),
         n = e,
         o = n.match(new RegExp(`^<${ZPe}(?: from="([${N6o}]+)")?(?: from-session="(${t})")?(?: hop-chain="(${r})")?(?: from-name="([^"<>\\n\\r]+)")?(?: from-mode="(${oSd.join("|")})")?>\\n([\\s\\S]*)\\n</${ZPe}>$`));
     if (!o) return;
     let i = o[3] !== void 0 ? o[3].split(",") : void 0,
         s = o[5];
     // Strict roundtrip validation: reconstructed tag must match byte-for-byte!
     if (NMr(o[1], o[4], o[6] ?? "", o[2], i, s) !== n) return;
     return {
       ...o[1] !== void 0 && { from: o[1] },
       ...o[2] !== void 0 && { fromSession: o[2] },
       ...i !== void 0 && { hopChain: i },
       ...o[4] !== void 0 && { fromName: o[4] },
       ...s !== void 0 && { fromMode: s },
       body: o[6] ?? ""
     };
   }
   ```
2. **Origin Tagging**: **[OBSERVED @ 279945911 (`gIt`), 288561374 (`tVn`), 288562285 (`vFf`)]**
   The parsed tokens are stored in the message origin object:
   ```javascript
   // Offset 279945911
   function gIt(e) {
     let t = pSd(e);
     if (!t) return {};
     let r = t.fromName ? fse(t.fromName) : "";
     return {
       ...r && { name: r },
       ...t.fromSession !== void 0 && { fromSession: t.fromSession },
       ...t.hopChain !== void 0 && { hopChain: t.hopChain },
       ...t.fromMode !== void 0 && { fromMode: t.fromMode },
       body: t.body
     };
   }

   // Offset 288561374
   function tVn(e, t, r, n, o, i) {
     let s = Uur(e);
     if (s) return { kind: "peer", from: s, inbound_origin: r, ...gIt(e) };
     ...
   }
   ```
3. **Prompt Stripping**: **[OBSERVED @ 279945694 (`cZs`), 284457735 (`oUp`)]**
   Before presenting the prompt to the model context, `hop-chain` is stripped from the prompt body via `cZs`, but the array is preserved in the queue origin metadata (`origin.hopChain`):
   ```javascript
   // Offset 279945694
   function cZs(e) {
     let t = pSd(e);
     if (!t || t.hopChain === void 0) return e;
     return NMr(t.from, t.fromName, t.body, t.fromSession, void 0, t.fromMode); // void 0 for hopChain!
   }
   ```

### Outbound Write Site
1. **Conversation History Inspection**: **[OBSERVED @ 279946173, function `yIt`]**
   When the agent prepares an outgoing peer send, `yIt` scans backwards through conversation messages to find the most recent user turn:
   ```javascript
   function yIt(e) {
     for (let t = e.length - 1; t >= 0; t--) {
       let r = e[t];
       if (r.type !== "user" || r.toolUseResult || r.isCompactSummary) continue;
       return r.origin?.kind === "peer" ? r.origin.hopChain ?? [] : void 0;
     }
     return;
   }
   ```
2. **Accumulation & Truncation**: **[OBSERVED @ 279944489, function `F6o`]**
   The accumulator appends this session's hop token (`mOt(l)`) to the existing chain and caps length to 32 (`LMr`):
   ```javascript
   function F6o(e, t) {
     if (e === void 0) return;
     let r = [...e];
     if (t) r.push(t);
     if (r.length === 0) return;
     return r.length > LMr ? r.slice(r.length - LMr) : r; // LMr = 32
   }
   ```
3. **Outbound Tag Construction**: **[OBSERVED @ 279944623 (`Hdb`), 281148350 (`CDn`), 288739698]**
   ```javascript
   // Offset 288739698 (peer send tool execution)
   let { msgId: I } = await v(p.sock, y, E, void 0, yIt(t.messages), a);

   // Offset 281148350 (sendToUdsSocket)
   async function CDn(e, t, r, n, o, i, { trackReceipts: s = !0 } = {}) {
     let a = gOt(),
         l = a ? Sdt(a) : void 0, // l = "uds:" + sSd(socketPath)
         c = NMr(l, r, t, void 0, F6o(o, l ? mOt(l) : void 0), i),
     ...
   }

   // Offset 279944623 (Hdb attribute formatter)
   function Hdb(e, t, r, n, o) {
     let i = [];
     if (e) i.push(`from="${e}"`);
     if (r && aSd.test(r)) i.push(`from-session="${r}"`);
     if (n !== void 0 && n.length > 0) {
       let a = n.join(",");
       if (uSd.test(a)) i.push(`hop-chain="${a}"`);
     }
     ...
     return i.length > 0 ? ` ${i.join(" ")}` : "";
   }
   ```

---

## 3. Loop Detection & Limit Enforcement (Q3)

Loop detection is **active and enforced**. Messages exceeding limits are **silently dropped**.

### Admission Check Site
- **Admission Gate**: **[OBSERVED @ 284460687, function `G`]**
  ```javascript
  function G(je) {
    let Me = je.origin;
    if (!rMa(Me)) return !0;
    let We = je.admissionExempt === "resurrected";
    if (!We && !iUp(Me)) {
      let qe = W.checkHopChain(Me.hopChain, Rla());
      if (qe && !qe.admitted)
        return Y.report({ reason: qe.reason, from: Me.from, ...Me.name !== void 0 && { name: Me.name } }), !1;
    }
    if (!We && iUp(Me)) {
      let qe = typeof je.value === "string" ? je.value : xd(je.value),
          Ue = W.admit({
            senderKey: Me.from !== "unknown" ? `from:${Me.from}` : `pid:${Me.verifiedPeerPid}`,
            body: qe,
            hopChain: Me.hopChain,
            ownTokens: Rla()
          });
      if (!Ue.admitted)
        return Y.report({ reason: Ue.reason, from: Me.from, ...Me.name !== void 0 && { name: Me.name } }), !1;
    }
    if (J() >= JHr().maxQueuedPeerMessages)
      return Y.report({ reason: "queue-full", from: Me.from, ...Me.name !== void 0 && { name: Me.name } }), !1;
    return be("peer_loop_guard"), !0;
  }
  ```

### Limit Enforcement Function
- **Check Logic**: **[OBSERVED @ 281140643 (`s`), 281140324 (`ALb`), 281141336 (`vla`)]**
  ```javascript
  // Offset 281141336 (Defaults)
  vla = {
    bucketCapacity: 30,
    refillPerSecond: 0.5,
    dedupWindowMs: 30000,
    maxSelfHops: 10,
    maxChainLength: 28,
    maxTrackedSenders: 256
  };

  // Offset 281140324 (Count self tokens)
  function ALb(e, t) {
    if (!e || t.size === 0) return 0;
    let r = 0;
    for (let n of e) if (t.has(n)) r++;
    return r;
  }

  // Offset 281140643 (s in Tla)
  function s(l, c) {
    let u = n();
    if (l !== void 0 && l.length > u.maxChainLength)
      return { admitted: !1, reason: "hop-runaway" };
    if (ALb(l, c) >= u.maxSelfHops)
      return { admitted: !1, reason: "hop-loop" };
    return;
  }
  ```

### Loop Guard Conditions & Triggers
1. **Hop Runaway (`hop-runaway`)**:
   - **Condition**: `hopChain.length > maxChainLength`
   - **Limit $N$**: `28` hops (default; schema bounds allow 8 to 31).
   - **Trip Result**: Rejection with reason `"hop-runaway"`.
2. **Self Loop (`hop-loop`)**:
   - **Condition**: `ALb(hopChain, ownTokens) >= maxSelfHops`
   - **Limit $N$**: `10` self-visits (default; schema bounds allow 3 to 32).
   - **Explanation**: `ownTokens` is the `Set` returned by `Rla()` (offset 281141586), containing this session's own tokens (`mOt(ownAddress)`). A session permits its own token to appear up to 9 times; on the 10th visit, it trips.
   - **Trip Result**: Rejection with reason `"hop-loop"`.

### What Happens on Trip
- **Silent Drop**: `G(je)` returns `false`. Neither `n.push(...)` nor queue notification is called. The message is completely dropped.
- **Logging & Telemetry**: **[OBSERVED @ 281142098, `xla`]**
  ```javascript
  T(`[peer-guard] Dropped peer message from ${a.from}${a.name ? ` (@${a.name})` : ""}: ${i.reason}${u > 0 ? ` (+${u} suppressed)` : ""}`, { level: "warn" });
  ve("peer_loop_guard", i.reason);
  Qh().ingress.messageDropped.emit(d);
  ```
- **No Error Frame**: No negative response or error frame is transmitted back across the UDS socket.

---

## 4. Origination Branch (Q4)

Why does an initial human-initiated send omit `hop-chain` while an agent reply includes it?

- **Origination Branch**: **[OBSERVED @ 279946173 (`yIt`), 279944489 (`F6o`)]**
  ```javascript
  // Offset 279946173
  function yIt(e) {
    for (let t = e.length - 1; t >= 0; t--) {
      let r = e[t];
      if (r.type !== "user" || r.toolUseResult || r.isCompactSummary) continue;
      return r.origin?.kind === "peer" ? r.origin.hopChain ?? [] : void 0;
    }
    return;
  }
  ```
- **Data Flow Evaluation**:
  1. **Human-initiated send**:
     - The latest user turn in `t.messages` was typed by the human operator (`r.origin?.kind !== "peer"`).
     - `yIt(t.messages)` returns `undefined` (`void 0`).
     - In `CDn`: `F6o(undefined, ownToken)` is invoked.
     - First line of `F6o`: `if (e === void 0) return;` -> returns `undefined`.
     - In `Hdb`: `if (n !== void 0 && n.length > 0)` evaluates to `false`.
     - **Result**: No `hop-chain` attribute is rendered on the XML wrapper.
  2. **Agent reply**:
     - The latest user turn was delivered by a peer message (`r.origin?.kind === "peer"`).
     - If the peer message had no `hop-chain`, `r.origin.hopChain` is `undefined`, so `r.origin.hopChain ?? []` evaluates to `[]`.
     - In `CDn`: `F6o([], ownToken)` is invoked.
     - `F6o` executes: `r = [...e]; r.push(t);` -> `r` is `[ ownToken ]`.
     - In `Hdb`: `n` is `[ ownToken ]`, so `hop-chain="<token>"` is emitted.

---

## 5. Hop Identity & Secrets (Q5)

- **What identifies a hop**:
  - The hop token is `HMAC-SHA256(key=kLb, msg=ownAddress).hex().slice(0, 24)`.
  - `ownAddress` is the session address string (e.g. `"uds:/tmp/cc-socks/25792.sock"`).
  - `kLb` is a **32-byte secret random key generated at process startup**.
- **Process Scope**:
  - Because `kLb` is held only in memory by that single process, **only the process that issued a token can recognize it as self**.
  - No foreign session or relay can calculate, match, or forge another session's self-token.
  - Foreign tokens in `hop-chain` are treated as opaque 24-hex identifiers.

---

## 6. Relay Implementation Rules

To bridge cross-session messages between hosts without breaking loop detection or message delivery, a relay must obey the following rules:

1. **Preserve Valid Attributes & XML Formatting Verbatim**:
   - **[OBSERVED @ 279945065]**: `pSd` validates that re-serializing parsed fields with `NMr` equals the original string byte-for-byte (`if (NMr(...) !== n) return;`). Any whitespace alteration, reordering, or attribute formatting change causes parsing failure and rejection.
   - Forward `<cross-session-message ...>` attributes (including `hop-chain`) intact.

2. **Do Not Synthesize Foreign Hop Tokens**:
   - The relay does not need to append its own token. If it does append a token, it MUST match `[0-9a-f]{24}` and adhere to the comma-separated format.
   - The relay MUST NOT inject an arbitrary or recycled token that could collide with a target session's internal tokens (though collision probability with a 12-byte HMAC is negligible).

3. **Manage Chain Length for Extended Ping-Pong**:
   - **[OBSERVED @ 281140643, 281141336]**: If two bridged agents engage in more than 28 replies, `hop-runaway` (`chain.length > 28`) will trigger and Claude Code will silently drop all subsequent messages.
   - Furthermore, if a single agent session processes 10 incoming turns from the same chain, `hop-loop` (`selfHops >= 10`) will trigger and drop incoming messages.
   - **Relay Strategy**: If the relay is facilitating extended back-and-forth communication between agents (e.g. review ping-pong), the relay can trim older tokens from the start of the `hop-chain` list (preserving at least the most recent tokens, or resetting when length exceeds e.g. 20) to prevent hitting `maxChainLength` (28) and `maxSelfHops` (10).

---

## Summary Table of Verified Offsets

| Component | Identifier | Byte Offset | Classification |
|---|---|---|---|
| Process Secret Key Gen | `kLb = b5d.randomBytes(32)` | 281139747 | OBSERVED |
| HMAC-SHA256 Slice Helper | `function cSd(e, t)` | 279944365 | OBSERVED |
| Own Hop Token Derivation | `function mOt(e)` | 281139659 | OBSERVED |
| Hop Chain Token Regex | `rSd`, `Mdb`, `uSd` | 279946659, 279946718 | OBSERVED |
| Hop Accumulator / Slicer | `function F6o(e, t)` | 279944489 | OBSERVED |
| Tag Attribute Builder | `function Hdb(e, t, r, n, o)` | 279944623 | OBSERVED |
| Tag Wrapper Builder | `function NMr(e, t, r, n, o, i)` | 279944973 | OBSERVED |
| Inbound Tag Parser | `function pSd(e)` | 279945065 | OBSERVED |
| Tag Stripper (for model prompt) | `function cZs(e)` | 279945694 | OBSERVED |
| Inbound Origin Builder | `function gIt(e)` | 279945911 | OBSERVED |
| Reverse Scan for Peer Origin | `function yIt(e)` | 279946173 | OBSERVED |
| Ingress Token Registration | `function kla(e)` | 281141457 | OBSERVED |
| Ingress Token Resolver | `function Rla()` | 281141586 | OBSERVED |
| Self-Hop Counter | `function ALb(e, t)` | 281140324 | OBSERVED |
| Guard Limit Defaults | `vla` (`maxChainLength: 28`, `maxSelfHops: 10`) | 281141336 | OBSERVED |
| Guard Check Function | `s(l, c)` in `Tla` | 281140643 | OBSERVED |
| Ingress Admission Gate | `function G(je)` | 284460687 | OBSERVED |
| Inbound Queue Processor | `function oUp(e)` | 284457735 | OBSERVED |
| Outbound UDS Send Function | `async function CDn(...)` | 281148350 | OBSERVED |
| Outbound Send Invocation | Call to `v(...)` in peer tool | 288739698 | OBSERVED |
| Drop Logger & Telemetry | `function xla()` | 281142098 | OBSERVED |
| Drop Reason Descriptions | `ILb` | 281143148 | OBSERVED |
