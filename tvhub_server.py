"""TV-Hub bridge: TCP port 50001 <-> browser GUI.

    python tvhub_server.py                        # live; dish IP as last set in the GUI
    python tvhub_server.py --host 192.168.50.214  # live, overriding the saved dish IP
    python tvhub_server.py --read-only            # live, never send anything
    python tvhub_server.py --replay tvhub.log     # view a saved capture

Serves tvhub_gui.html at http://127.0.0.1:8650/ and streams parsed records to it with
Server-Sent Events. The dish (TV-Hub) address can be changed from the GUI; the last one
used is kept in tvhub_config.json. Live sessions are written to logs/ with arrival
timestamps (the format --replay and tvhub_parser.py read back). Stdlib only.

Commands from the GUI are checked here against an explicit allowlist: the bare
read-only queries the TV-Hub itself sends at boot, plus SAT,<sat>,<H|V>,<L|H> for a
satellite already seen in this session's telemetry. Nothing else is ever sent, and
nothing is ever sent on a timer - only when someone clicks.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import queue
import re
import socket
import sys
import threading
import time
import uuid
import webbrowser
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from tvhub_parser import LineSplitter, format_log_line, make_record, read_log

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_FILE = os.path.join(HERE, "tvhub_gui.html")
CONFIG_FILE = os.path.join(HERE, "tvhub_config.json")
DEFAULT_HOST, DEFAULT_PORT = "192.168.50.214", 50001

# Bare words the TV-Hub sends at boot (plus VERSION, seen in a capture): read-only queries.
QUERY_COMMANDS = (
    "STATE", "SAT", "SATINSTALL", "VERSION", "GPS", "HOURS", "STATUS", "ANTLNB", "SIDELOBE",
    "SLEEP", "SEARCHTIMEOUT", "HW", "@VER", "@FPGAVER", "=SERNUM",
)
_RE_SAT_SET = re.compile(r"^SAT,([A-Z0-9]{1,12}),([HV]),([LH])$")
# Records whose "sat" field names a satellite the antenna knows about.
_SAT_KINDS = {"pos", "rf_freq", "rf_satconfig", "rf_satinstall", "rf_select", "sat_sel", "satinstall",
              "search_target", "mode", "satsetup", "satconfig"}
_RE_HOSTNAME = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                          r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?$")
MIN_COMMAND_INTERVAL_S = 0.5
EOLS = {"crlf": "\r\n", "lf": "\n", "cr": "\r"}


class RequestRefused(Exception):
    """A GUI request the bridge won't carry out; .status is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


class CommandRejected(RequestRefused):
    pass


def validate_command(cmd: str, known_sats: set[str]) -> str:
    """Return the normalised command if it is on the allowlist, else raise CommandRejected."""
    c = cmd.strip().upper()
    if c in QUERY_COMMANDS:
        return c
    m = _RE_SAT_SET.match(c)
    if m:
        if m.group(1) not in known_sats:
            raise CommandRejected(f"satellite {m.group(1)} has not appeared in the telemetry this session")
        return c
    raise CommandRejected("not on the allowlist")


def validate_target(host, port) -> tuple[str, int]:
    """Normalise a dish address from the GUI or config, else raise RequestRefused."""
    host = str(host).strip()
    try:
        host = str(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        if not _RE_HOSTNAME.match(host) or re.fullmatch(r"[\d.]+", host):
            raise RequestRefused(f"{host[:60]!r} is not an IP address or host name", 400) from None
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise RequestRefused("the port must be a number", 400) from None
    if not 1 <= port <= 65535:
        raise RequestRefused("the port must be between 1 and 65535", 400)
    return host, port


def load_config(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(path: str, data: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
    except OSError as e:
        print(f"could not save {path}: {e}", file=sys.stderr)


class _Client:
    def __init__(self) -> None:
        self.q: queue.Queue = queue.Queue(maxsize=20000)
        self.dropped = False


class Hub:
    """Record history + fan-out to SSE clients + session log file."""

    def __init__(self, history: int, log_path: str | None) -> None:
        self._lock = threading.Lock()
        self._backlog: deque[str] = deque(maxlen=history)
        self._clients: set[_Client] = set()
        self._seq = 0
        self.session = uuid.uuid4().hex[:12]
        self.status = {"link": "starting", "text": "", "since": time.time()}
        self.known_sats: set[str] = set()
        self.log_path = log_path
        self._log = open(log_path, "a", encoding="utf-8", buffering=1) if log_path else None

    def publish(self, t: float, src: str, text: str, log: bool = True, guard=None) -> dict | None:
        """Parse and fan out one line. `guard` is checked under the lock: a line from a
        connection that has just been replaced is dropped instead of leaking into the
        next session."""
        with self._lock:
            if guard is not None and not guard():
                return None
            return self._publish_locked(t, src, text, log)

    def _publish_locked(self, t: float, src: str, text: str, log: bool) -> dict:
        rec = make_record(text, t, src, self._seq)
        self._seq += 1
        if rec["kind"] in _SAT_KINDS:
            for name in rec.get("sats") or [rec.get("sat")]:
                if name:
                    self.known_sats.add(name.upper())
        data = json.dumps(rec, separators=(",", ":"))
        self._backlog.append(data)
        self._broadcast("rec", data)
        if log and self._log:
            self._log.write(format_log_line(t, src, text) + "\n")
        return rec

    def note(self, text: str, t: float | None = None) -> None:
        self.publish(time.time() if t is None else t, "bridge", text)

    def set_status(self, link: str, text: str = "") -> None:
        with self._lock:
            if self.status["link"] != link:
                self.status["since"] = time.time()
            self.status.update(link=link, text=text)
            self._broadcast("status", json.dumps(self.status))

    def new_session(self, before=None, hello=None, note: str | None = None) -> None:
        """Start over, e.g. for a different dish: `before` runs with publishing held off,
        the history and the seen-satellite list are dropped, and connected browsers get a
        fresh hello (new session id) so they reset themselves."""
        with self._lock:
            if before is not None:
                before()
            self._backlog.clear()
            self.known_sats.clear()
            self.session = uuid.uuid4().hex[:12]
            if hello is not None:
                self._broadcast("hello", json.dumps(hello(dict(self.status))))
                self._broadcast("ready", "{}")
            if note:
                self._publish_locked(time.time(), "bridge", note, True)

    def _broadcast(self, kind: str, data: str) -> None:  # caller holds the lock
        for client in list(self._clients):
            try:
                client.q.put_nowait((kind, data))
            except queue.Full:  # browser tab stopped reading; let it reconnect
                client.dropped = True
                self._clients.discard(client)

    def subscribe(self) -> tuple[_Client, list[str], dict, str]:
        client = _Client()
        with self._lock:
            self._clients.add(client)
            return client, list(self._backlog), dict(self.status), self.session

    def unsubscribe(self, client: _Client) -> None:
        with self._lock:
            self._clients.discard(client)

    def close(self) -> None:
        if self._log:
            self._log.close()
            self._log = None


def _enable_keepalive(sock: socket.socket) -> None:
    """Notice a dead TV-Hub within about a minute instead of the OS default of hours."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    try:
        if hasattr(socket, "SIO_KEEPALIVE_VALS"):  # Windows
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 30_000, 10_000))
        else:
            for name, value in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
                if hasattr(socket, name):
                    sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
    except OSError:
        pass


class LiveSource(threading.Thread):
    """Keeps a connection to the TV-Hub open, reconnecting with backoff."""

    def __init__(self, hub: Hub, host: str, port: int, eol: str) -> None:
        super().__init__(name="tvhub-live", daemon=True)
        self.hub, self.host, self.port, self.eol = hub, host, port, eol
        self._sock: socket.socket | None = None
        self._sock_lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._wake = threading.Event()  # cuts a retry wait short
        self._gen = 0                   # bumped by retarget(); older connections stop publishing
        self._reconnect_now = False

    def run(self) -> None:
        backoff, last_error, last_gen = 1.0, None, -1
        while not self._stop_evt.is_set():
            self._wake.clear()
            with self._sock_lock:
                host, port, gen = self.host, self.port, self._gen
            if gen != last_gen:
                backoff, last_error, last_gen = 1.0, None, gen
            target = f"{host}:{port}"
            self.hub.set_status("connecting", f"connecting to {target}")
            try:
                sock = socket.create_connection((host, port), timeout=5)
            except OSError as e:
                if gen != self._gen:
                    continue
                if str(e) != last_error:  # don't repeat the same failure every retry
                    self.hub.note(f"connect to {target} failed: {e}")
                    last_error = str(e)
                self.hub.set_status("disconnected", f"connect to {target} failed: {e} (retrying in {backoff:.0f} s)")
                self._wake.wait(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            _enable_keepalive(sock)
            sock.settimeout(0.5)
            with self._sock_lock:
                if gen != self._gen:  # retargeted while we were connecting
                    sock.close()
                    continue
                self._sock = sock
                self._reconnect_now = False
            backoff, last_error = 1.0, None
            self.hub.note(f"connected to {target}")
            self.hub.set_status("connected", target)
            try:
                self._read_loop(sock, gen)
                reason = "bridge stopping"
            except OSError as e:
                reason = str(e) or e.__class__.__name__
            finally:
                with self._sock_lock:
                    if self._sock is sock:
                        self._sock = None
                    requested, self._reconnect_now = self._reconnect_now, False
                try:
                    sock.close()
                except OSError:
                    pass
            if self._stop_evt.is_set():
                break
            if gen != self._gen:
                continue  # new dish address: its session starts clean
            if requested:
                self.hub.note("reconnecting (requested from the GUI)")
                continue
            self.hub.note(f"disconnected from {target}: {reason}")
            self.hub.set_status("disconnected", reason)
            self._wake.wait(backoff)

    def _read_loop(self, sock: socket.socket, gen: int) -> None:
        splitter = LineSplitter()
        last_rx = time.time()

        def current() -> bool:
            return self._gen == gen

        while not self._stop_evt.is_set() and current():
            try:
                data = sock.recv(4096)
            except socket.timeout:
                # A line with no terminator (a prompt?) would otherwise glue onto the next one.
                if splitter.pending and time.time() - last_rx > 0.5:
                    for line in splitter.flush():
                        self.hub.publish(time.time(), "rx", line, guard=current)
                continue
            if not data:
                raise ConnectionError("connection closed by the TV-Hub")
            last_rx = time.time()
            for line in splitter.feed(data):
                self.hub.publish(last_rx, "rx", line, guard=current)

    def send(self, command: str) -> None:
        with self._sock_lock:
            if self._sock is None:
                raise CommandRejected("not connected to the TV-Hub", 503)
            try:
                self._sock.sendall((command + self.eol).encode("ascii"))
            except OSError as e:
                raise CommandRejected(f"send failed: {e}", 503) from e

    def retarget(self, host: str, port: int) -> None:
        """Switch to another dish address: drop the current connection and connect there."""
        with self._sock_lock:
            self.host, self.port = host, port
            self._gen += 1
            if self._sock is not None:
                try:
                    self._sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self._wake.set()

    def reconnect(self) -> None:
        with self._sock_lock:
            if self._sock is not None:
                self._reconnect_now = True
                try:
                    self._sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self._wake.set()

    def stop(self) -> None:
        self._stop_evt.set()
        self.reconnect()


class ReplaySource(threading.Thread):
    """Feeds a saved capture through the hub, instantly (speed 0) or paced."""

    def __init__(self, hub: Hub, path: str, entries: list, timing: str, speed: float) -> None:
        super().__init__(name="tvhub-replay", daemon=True)
        self.hub, self.path, self.entries, self.timing, self.speed = hub, path, entries, timing, speed
        self._stop_evt = threading.Event()

    def run(self) -> None:
        name = os.path.basename(self.path)
        if not self.entries:
            self.hub.set_status("replay", f"{name}: empty")
            return
        self.hub.set_status("replay", f"{name} · {len(self.entries)} lines · {self.timing} timestamps")
        prev = None
        for t, src, text in self.entries:
            if self.speed > 0 and prev is not None:
                if self._stop_evt.wait(min(max(t - prev, 0) / self.speed, 5.0)):
                    return
            self.hub.publish(t, src, text, log=False)
            prev = t
        self.hub.set_status("replay-done", f"{name} · {len(self.entries)} lines · {self.timing} timestamps")

    def stop(self) -> None:
        self._stop_evt.set()


class App:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.mode = "replay" if args.replay else "live"
        self.read_only = args.read_only or self.mode == "replay"
        self._cmd_lock = threading.Lock()
        self._target_lock = threading.Lock()
        self._last_cmd = 0.0
        if self.mode == "replay":
            entries, timing = read_log(args.replay)
            self.hub = Hub(max(args.history, len(entries) + 100), None)
            self.source = ReplaySource(self.hub, args.replay, entries, timing, args.speed)
        else:
            log_path = None
            if not args.no_log:
                os.makedirs(args.log_dir, exist_ok=True)
                log_path = os.path.join(args.log_dir, datetime.now().strftime("tvhub-%Y%m%d-%H%M%S.log"))
            self.hub = Hub(args.history, log_path)
            self.source = LiveSource(self.hub, args.host, args.port, EOLS[args.eol])

    def hello(self, status: dict) -> dict:
        a = self.args
        info = {"session": self.hub.session, "mode": self.mode, "status": status,
                "commands": not self.read_only, "queries": list(QUERY_COMMANDS)}
        if self.mode == "live":
            info.update(host=a.host, port=a.port, log_path=self.hub.log_path, read_only=a.read_only)
        else:
            info.update(file=os.path.basename(a.replay))
        return info

    def send_command(self, cmd: str) -> str:
        if self.mode != "live":
            raise CommandRejected("commands are disabled while replaying a log", 409)
        if self.read_only:
            raise CommandRejected("the bridge was started with --read-only", 409)
        try:
            command = validate_command(cmd, self.hub.known_sats)
        except CommandRejected as e:
            shown = re.sub(r"[^\x20-\x7e]", "?", cmd.strip())[:60]
            self.hub.note(f"refused to send {shown!r}: {e}")
            raise
        with self._cmd_lock:
            now = time.monotonic()
            if now - self._last_cmd < MIN_COMMAND_INTERVAL_S:
                raise CommandRejected("commands are limited to two per second", 429)
            self.source.send(command)
            self._last_cmd = now
            self.hub.publish(time.time(), "tx", command)
        return command

    def set_target(self, host, port) -> tuple[str, int]:
        """Point the bridge at another dish (TV-Hub). A new address starts a new session:
        history and seen satellites belong to the old dish, so both are dropped."""
        if self.mode != "live":
            raise RequestRefused("replaying a log: restart tvhub_server.py without --replay to connect to a dish", 409)
        host, port = validate_target(host, port)
        with self._target_lock:
            if (host, port) == (self.args.host, self.args.port):
                self.source.reconnect()
                return host, port
            self.args.host, self.args.port = host, port
            self.hub.new_session(before=lambda: self.source.retarget(host, port), hello=self.hello,
                                 note=f"dish address set to {host}:{port}")
            save_config(self.args.config, {"host": host, "port": port})
        return host, port

    def reconnect(self) -> None:
        if self.mode != "live":
            raise RequestRefused("not in live mode", 409)
        self.source.reconnect()

    def close(self) -> None:
        self.source.stop()
        self.hub.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "tvhub-bridge/1"
    app: App  # set on the per-server subclass by make_server() / main()
    allowed_hosts: set[str] | None = None

    def log_request(self, code="-", size="-") -> None:
        pass

    def _host_ok(self) -> bool:
        # Loopback binding: only answer to loopback names (blocks DNS-rebinding pages).
        return self.allowed_hosts is None or self.headers.get("Host", "") in self.allowed_hosts

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: dict) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json")

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._json(403, {"error": "unexpected Host header"})
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(GUI_FILE, "rb") as fh:
                    body = fh.read()
            except OSError as e:
                return self._json(500, {"error": f"cannot read {GUI_FILE}: {e}"})
            return self._send(200, body, "text/html; charset=utf-8")
        if path == "/api/events":
            return self._sse()
        if path == "/api/status":
            hub = self.app.hub
            return self._json(200, {**self.app.hello(dict(hub.status)), "known_sats": sorted(hub.known_sats)})
        self._json(404, {"error": "not found"})

    def do_HEAD(self) -> None:
        found = self._host_ok() and urlsplit(self.path).path in ("/", "/index.html")
        self.send_response(200 if found else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

    def _read_json(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 4096:
            self._json(413, {"error": "too large"})
            return None
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            self._json(400, {"error": "bad JSON"})
            return None
        return body

    def do_POST(self) -> None:
        if not self._host_ok():
            return self._json(403, {"error": "unexpected Host header"})
        # Cross-site pages can't set a JSON content type without a CORS preflight we never
        # answer, and browsers always send Origin on cross-origin POSTs.
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            return self._json(403, {"error": "cross-origin request refused"})
        if self.headers.get_content_type() != "application/json":
            return self._json(415, {"error": "expected application/json"})
        path = urlsplit(self.path).path
        if path not in ("/api/command", "/api/target", "/api/reconnect"):
            return self._json(404, {"error": "not found"})
        body = self._read_json()
        if body is None:
            return
        try:
            if path == "/api/command":
                return self._json(200, {"ok": True, "sent": self.app.send_command(str(body.get("cmd", "")))})
            if path == "/api/target":
                host, port = self.app.set_target(body.get("host", ""), body.get("port", DEFAULT_PORT))
                return self._json(200, {"ok": True, "host": host, "port": port})
            self.app.reconnect()
            return self._json(200, {"ok": True})
        except RequestRefused as e:
            return self._json(e.status, {"error": str(e)})

    def _event(self, name: str | None, data: str) -> None:
        head = f"event: {name}\n" if name else ""
        self.wfile.write(f"{head}data: {data}\n\n".encode("utf-8"))

    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        hub = self.app.hub
        client, backlog, status, session = hub.subscribe()
        try:
            hello = self.app.hello(status)
            hello["session"] = session  # the one this backlog belongs to
            self._event("hello", json.dumps(hello))
            for i in range(0, len(backlog), 2000):
                self._event("backlog", "[" + ",".join(backlog[i:i + 2000]) + "]")
            self._event("ready", "{}")
            while not client.dropped:
                try:
                    kind, data = client.q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    continue
                self._event(None if kind == "rec" else kind, data)
        except OSError:
            pass  # browser went away
        finally:
            hub.unsubscribe(client)


def _loopback_hosts(http_host: str, port: int) -> set[str] | None:
    try:
        loopback = ipaddress.ip_address(http_host).is_loopback
    except ValueError:
        loopback = http_host == "localhost"
    if not loopback:
        return None
    return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}


def make_server(app: App | None, http_host: str, http_port: int) -> ThreadingHTTPServer:
    """Bind the GUI server (port 0 = any free port). Set .app on the handler before serving."""
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer((http_host, http_port), handler)
    httpd.daemon_threads = True
    handler.allowed_hosts = _loopback_hosts(http_host, httpd.server_address[1])
    return httpd


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TV-Hub port-50001 bridge and GUI server.")
    ap.add_argument("--host", help=f"dish (TV-Hub) address; default: the last one set in the GUI, else {DEFAULT_HOST}")
    ap.add_argument("--port", type=int, help=f"TV-Hub maintenance port (default {DEFAULT_PORT})")
    ap.add_argument("--replay", metavar="LOG", help="show a saved capture instead of connecting")
    ap.add_argument("--speed", type=float, default=0,
                    help="replay pacing: 0 = load instantly (default), 1 = real time, 10 = 10x")
    ap.add_argument("--read-only", action="store_true", help="never send anything to the TV-Hub")
    ap.add_argument("--eol", choices=sorted(EOLS), default="crlf", help="line ending for commands (default crlf)")
    ap.add_argument("--http-host", default="127.0.0.1", help="GUI bind address (default %(default)s)")
    ap.add_argument("--http-port", type=int, default=8650, help="GUI port (default %(default)s)")
    ap.add_argument("--log-dir", default=os.path.join(HERE, "logs"), help="where live sessions are logged")
    ap.add_argument("--no-log", action="store_true", help="don't write a session log")
    ap.add_argument("--history", type=int, default=50000,
                    help="records kept for browsers that connect later (default %(default)s, about 10 h)")
    ap.add_argument("--config", default=CONFIG_FILE, help="where the dish address set in the GUI is saved")
    ap.add_argument("--no-browser", action="store_true", help="don't open the GUI automatically")
    args = ap.parse_args(argv)

    if args.replay and not os.path.exists(args.replay):
        ap.error(f"no such file: {args.replay}")
    saved = load_config(args.config)
    try:
        saved_host, saved_port = validate_target(saved.get("host", DEFAULT_HOST), saved.get("port", DEFAULT_PORT))
    except RequestRefused:
        saved_host, saved_port = DEFAULT_HOST, DEFAULT_PORT
    try:
        args.host, args.port = validate_target(args.host or saved_host, args.port or saved_port)
    except RequestRefused as e:
        ap.error(str(e))

    try:
        httpd = make_server(None, args.http_host, args.http_port)
    except OSError as e:
        print(f"cannot listen on {args.http_host}:{args.http_port}: {e}", file=sys.stderr)
        return 1
    app = App(args)
    httpd.RequestHandlerClass.app = app
    app.source.start()

    shown_host = "127.0.0.1" if args.http_host in ("", "0.0.0.0", "::") else args.http_host
    url = f"http://{shown_host}:{args.http_port}/"
    if app.mode == "live":
        print(f"TV-Hub {args.host}:{args.port} -> {url}"
              + ("  (read-only)" if args.read_only else "  (allowlisted commands enabled)"))
        if app.hub.log_path:
            print(f"logging to {app.hub.log_path}")
    else:
        print(f"replaying {args.replay} -> {url}")
    print("Ctrl+C to stop")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
