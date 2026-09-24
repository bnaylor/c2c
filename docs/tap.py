#!/usr/bin/env python3
"""
tap.py — observe Claude Code peer messaging on the wire.

Two modes, both of which stand up the three artifacts a real session publishes
(registry entry + key file + unix socket) so this process is a first-class peer:
appears in ListAgents, addressable by name, and reachable for reverse callbacks.

  sink  (default)   Pure receiver. Logs every frame sent TO us. Disturbs no
                    live session. Proves the receive half of a bridge.

  send  (--to X)    Sends a real `user` frame to a live session X, exactly as
                    SendMessage does, with `from` pointing back at our own
                    socket — so the target's receipts (peer_message_status:
                    held/delivered/denied) and idle notices land on our
                    listener and get logged. This DOES ping a live session.

Examples:
    python3 tap.py                          # sink named "tap-sink"
    python3 tap.py --to iris-ac --text "ping from tap"        # send + watch receipts
    python3 tap.py --to iris-ac --notify --text "ping"        # also subscribe to idle

Ctrl-C to stop; all artifacts are removed on exit. Same-uid, local socket only.
"""
import argparse, atexit, hashlib, json, os, signal, socket, sys, threading, time, uuid

SESSIONS = os.path.expanduser("~/.claude/sessions")
SOCK_DIR = "/tmp/cc-socks"

def procstart_str():
    return time.strftime("%a %b %e %H:%M:%S %Y", time.localtime())

def detect_version():
    try:
        for fn in os.listdir(SESSIONS):
            if fn.endswith(".json"):
                v = json.load(open(os.path.join(SESSIONS, fn))).get("version")
                if v:
                    return v
    except Exception:
        pass
    return "2.1.236"

def key_filename(pid, sock_path):
    # protocol.md [OBS]: hash the path AS WRITTEN, no realpath.
    h = hashlib.sha256(sock_path.encode()).hexdigest()
    return os.path.join(SESSIONS, f"{pid}.{h}.key"), h

def resolve_target(needle):
    """Return (pid, sock_path, peer_token, name) for a live session by name or pid."""
    for fn in sorted(os.listdir(SESSIONS)):
        if not fn.endswith(".json"):
            continue
        try:
            d = json.load(open(os.path.join(SESSIONS, fn)))
        except Exception:
            continue
        if d.get("pid") == os.getpid():
            continue
        if str(d.get("pid")) == needle or d.get("name") == needle:
            sp = d.get("messagingSocketPath")
            kp, _ = key_filename(d["pid"], sp)
            if not os.path.exists(kp):
                sys.exit(f"[-] target {needle} found but key file missing: {kp}")
            tok = json.load(open(kp))["peerToken"]
            return d["pid"], sp, tok, d.get("name")
    sys.exit(f"[-] no live session matching {needle!r} in {SESSIONS}")

class Peer:
    """Our own published identity: registry + key + listening socket."""
    def __init__(self, name, cwd, logf):
        self.pid = os.getpid()
        self.sock_path = f"{SOCK_DIR}/{self.pid}.sock"
        self.name = name
        self.cwd = cwd
        self.logf = logf
        self.peer_token = os.urandom(16).hex()
        self.reg_path = os.path.join(SESSIONS, f"{self.pid}.json")
        self.key_path, self.key_hash = key_filename(self.pid, self.sock_path)
        self._registry = None
        self._srv = None

    def log(self, msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        self.logf.write(line + "\n")

    def publish(self):
        os.makedirs(SOCK_DIR, mode=0o700, exist_ok=True)
        os.makedirs(SESSIONS, mode=0o700, exist_ok=True)
        um = os.umask(0o077)
        with open(self.key_path, "w") as f:
            json.dump({"peerToken": self.peer_token, "procStart": procstart_str()}, f)
        os.chmod(self.key_path, 0o600)
        os.umask(um)
        now = int(time.time() * 1000)
        self._registry = {
            "pid": self.pid,
            "sessionId": f"00000000-0000-4000-8000-{self.pid:012d}",
            "cwd": self.cwd, "startedAt": now, "procStart": procstart_str(),
            "version": detect_version(), "peerProtocol": 1,
            "peerFeatures": ["notify_idle"], "kind": "interactive",
            "entrypoint": "cli", "messagingSocketPath": self.sock_path,
            "name": self.name, "nameSource": "derived", "nameSince": now,
            "status": "idle", "updatedAt": now, "statusUpdatedAt": now,
        }
        self._write_registry()
        os.chmod(self.reg_path, 0o644)
        if os.path.exists(self.sock_path):
            os.unlink(self.sock_path)
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(self.sock_path)
        os.chmod(self.sock_path, 0o600)
        self._srv.listen(8)
        atexit.register(self.cleanup)
        self.log(f"=== peer up: name={self.name!r} pid={self.pid} sock={self.sock_path} ===")
        self.log(f"    key hash (no-realpath) = {self.key_hash}")

    def _write_registry(self):
        with open(self.reg_path, "w") as f:
            json.dump(self._registry, f)

    def cleanup(self):
        for p in (self.sock_path, self.key_path, self.reg_path):
            try: os.unlink(p)
            except FileNotFoundError: pass
        try: self._srv.close()
        except Exception: pass
        self.log("--- stopped, artifacts removed ---")

    def serve_forever(self):
        """Accept connections; log every frame. Refreshes updatedAt on idle."""
        self._srv.settimeout(5.0)
        n = 0
        while True:
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                self._registry["updatedAt"] = int(time.time() * 1000)
                try: self._write_registry()
                except Exception: pass
                continue
            except OSError:
                return
            n += 1
            self._handle(conn, n)

    def _handle(self, conn, n):
        conn.settimeout(10.0)
        buf = b""; total = 0
        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk; total += len(chunk)
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                        tag = obj.get("action") or obj.get("type") or "?"
                        self.log(f"[in {n}] REVERSE FRAME <{tag}>: {json.dumps(obj, indent=2)}")
                    except Exception:
                        self.log(f"[in {n}] NON-JSON ({len(line)}b): {line!r}")
        except socket.timeout:
            self.log(f"[in {n}] read timeout")
        finally:
            self.log(f"[in {n}] closed, {total}b")
            conn.close()

    # --- send half ---
    def send_user(self, tsock, ttoken, tname, text, notify):
        wrapper = (
            f'<cross-session-message from="uds:{self.sock_path}" '
            f'from-name="{self.name}" from-mode="prompting">\n{text}\n'
            f'</cross-session-message>'
        )
        mid = str(uuid.uuid4())
        auth = json.dumps({"type": "auth", "token": ttoken}) + "\n"
        user = json.dumps({
            "msgV": 1, "msg_id": mid, "type": "user",
            "message": {"role": "user", "content": wrapper},
            "priority": "next", "from": f"uds:{self.sock_path}",
        }) + "\n"
        self.log(f"[out] -> {tname} ({tsock})  msg_id={mid}")
        self._one_shot(tsock, auth + user, "user")
        if notify:
            ctl = json.dumps({
                "msgV": 1, "msg_id": str(uuid.uuid4()), "type": "control",
                "action": "notify_when_idle", "from": f"uds:{self.sock_path}",
                "from_mode": "prompting",
            }) + "\n"
            self.log("[out] -> notify_when_idle subscription")
            self._one_shot(tsock, auth + ctl, "notify_when_idle")

    def _one_shot(self, tsock, payload, label):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5.0)
        try:
            s.connect(tsock)
        except OSError as e:
            self.log(f"[out] connect failed for {label}: {e}")
            return
        try:
            s.sendall(payload.encode("utf-8"))
            time.sleep(0.15)  # macOS client buffer-flush delay
            s.shutdown(socket.SHUT_WR)
            resp = b""
            try:
                while True:
                    c = s.recv(4096)
                    if not c: break
                    resp += c
            except socket.timeout:
                pass
            self.log(f"[out] {label} sent; sync reply bytes={len(resp)}"
                     + (f" data={resp!r}" if resp else ""))
        finally:
            s.close()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default=None, help="our peer name (default tap-sink / tap-sender)")
    ap.add_argument("--to", default=None, help="target session name or pid -> send mode")
    ap.add_argument("--text", default="ping from tap.py", help="message body (send mode)")
    ap.add_argument("--notify", action="store_true", help="also send notify_when_idle")
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--log", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "tap.log"))
    args = ap.parse_args()

    logf = open(args.log, "a", buffering=1)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    if args.to:  # SEND MODE
        peer = Peer(args.name or "tap-sender", args.cwd, logf)
        peer.publish()
        tpid, tsock, ttok, tname = resolve_target(args.to)
        peer.log(f"target: {tname} pid={tpid} sock={tsock} token={ttok[:6]}…")
        # listener runs in background to catch reverse callbacks
        t = threading.Thread(target=peer.serve_forever, daemon=True)
        t.start()
        time.sleep(0.3)
        peer.send_user(tsock, ttok, tname, args.text, args.notify)
        peer.log("sent. holding open to capture reverse frames — Ctrl-C to stop.")
        while True:
            time.sleep(3600)
    else:        # SINK MODE
        peer = Peer(args.name or "tap-sink", args.cwd, logf)
        peer.publish()
        peer.log(f"sink ready. From another session: SendMessage(to={peer.name!r}, message=...)")
        peer.serve_forever()

if __name__ == "__main__":
    main()
