"""TV-Hub bridge: TCP port 50001 <-> browser GUI.

    python tvhub_server.py                        # live; dish IP as last set in the GUI
    python tvhub_server.py --host 192.168.50.214  # live, overriding the saved dish IP
    python tvhub_server.py --read-only            # live, never send anything
    python tvhub_server.py --replay tvhub.log     # view a saved capture

Serves tvhub_gui.html at http://127.0.0.1:8650/ (with --lan: to the local network too, as
http://kvh.local/) and streams parsed records to it with Server-Sent Events. The dish (TV-Hub) address can be changed from the GUI; the last one
used is kept in tvhub_config.json. Live sessions are written to logs/ with arrival
timestamps (the format --replay and tvhub_parser.py read back). Stdlib only.

Commands from the GUI are checked here against an explicit allowlist:
- read-only queries: the bare words the TV-Hub sends at boot, plus HELP, TGTLOCATION and
  SIGLEVEL from the antenna's own HELP list
- SAT,<sat>,<H|V>,<L|H> for a satellite already seen in this session's telemetry
- switching to another satellite of the TV-Hub's installed group, through its web service
  (select_satellite, install=N; see tvhub_webservice.py), only while autoswitch is off
- HALT, TRACK and DEBUGON (the antenna refuses TRACK until DEBUGON after a reboot)
- manual pointing (AZ,<0-3599>, EL,<150-600>, the 0.1-degree steps 2/4/6/8), accepted
  only while the antenna reports Idle
Nothing else is ever sent (SMACK, ZAP, CLEAREE, =CAL... are refused), and nothing is sent
except in response to a click in the GUI.
"""

from __future__ import annotations

import argparse
import hashlib
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

import tvhub_lan as lan
import tvhub_webservice as webservice
from tvhub_parser import LineSplitter, format_log_line, make_record, read_log

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_FILE = os.path.join(HERE, "tvhub_gui.html")
CONFIG_FILE = os.path.join(HERE, "tvhub_config.json")
DEFAULT_HOST, DEFAULT_PORT = "192.168.50.214", 50001

# Read-only queries: the bare words the TV-Hub sends at boot, VERSION (seen in a capture),
# and the "Report ..." entries of the antenna's own HELP list.
QUERY_COMMANDS = (
    "STATE", "SAT", "SATINSTALL", "VERSION", "GPS", "HOURS", "STATUS", "ANTLNB", "SIDELOBE",
    "SLEEP", "SEARCHTIMEOUT", "HW", "@VER", "@FPGAVER", "=SERNUM", "HELP", "TGTLOCATION", "SIGLEVEL",
)
# HALT stops acquisition/tracking and enters Idle mode; TRACK resumes (both seen in use).
# After a reboot the antenna answers TRACK with "TRACK requires Debug mode." until it gets
# DEBUGON (seen in the captures; it only turns on extra diagnostic lines and is idempotent).
CONTROL_COMMANDS = ("HALT", "TRACK", "DEBUGON")
# Manual pointing, from HELP. Only allowed while the antenna reports Idle.
JOG_COMMANDS = {"8": "EL +0.1°", "2": "EL −0.1°", "6": "AZ +0.1° (CW)", "4": "AZ −0.1° (CCW)"}
_RE_MANUAL = re.compile(r"^(AZ|EL),(\d{1,4})$")
MANUAL_LIMITS = {"AZ": (0, 3599, 4), "EL": (150, 600, 3)}  # tenths of a degree, and field width
_RE_SAT_SET = re.compile(r"^SAT,([A-Z0-9]{1,12}),([HV]),([LH])$")
# Records whose "sat" field names a satellite the antenna knows about.
_SAT_KINDS = {"pos", "rf_freq", "rf_satconfig", "rf_satinstall", "rf_select", "sat_sel", "satinstall",
              "search_target", "mode", "satsetup", "satconfig"}
_RE_HOSTNAME = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                          r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?$")
MIN_COMMAND_INTERVAL_S = 0.5
EOLS = {"crlf": "\r\n", "lf": "\n", "cr": "\r"}

# The GUI is read from disk on every page load, so after an update it can be newer than a
# bridge that is still running. Bump this together with BRIDGE_API in tvhub_gui.html when
# the GUI starts relying on something new here; the page then asks for a restart.
BRIDGE_API = 2
_CODE_FILES = (os.path.abspath(__file__), os.path.join(HERE, "tvhub_parser.py"))


def _code_fingerprint() -> str:
    h = hashlib.sha1()
    for path in _CODE_FILES:
        try:
            with open(path, "rb") as fh:
                h.update(fh.read())
        except OSError:
            h.update(b"missing")
    return h.hexdigest()


CODE_FINGERPRINT = _code_fingerprint()  # the code this process is running
STARTED = time.time()


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
    if c in QUERY_COMMANDS or c in CONTROL_COMMANDS or c in JOG_COMMANDS:
        return c
    m = _RE_MANUAL.match(c)
    if m:
        lo, hi, width = MANUAL_LIMITS[m.group(1)]
        value = int(m.group(2))
        if not lo <= value <= hi:
            raise CommandRejected(f"{m.group(1)} must be {lo}-{hi} (tenths of a degree)")
        return f"{m.group(1)},{value:0{width}d}"  # zero-padded, as in HELP's AZ,XXXX / EL,XXX
    m = _RE_SAT_SET.match(c)
    if m:
        if m.group(1) not in known_sats:
            raise CommandRejected(f"satellite {m.group(1)} has not appeared in the telemetry this session")
        return c
    raise CommandRejected("not on the allowlist")


def is_manual_move(command: str) -> bool:
    return command in JOG_COMMANDS or command.startswith(("AZ,", "EL,"))


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
        self.antenna_state: str | None = None  # last +STATE / >STATE seen, e.g. "Idle"
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
        if rec["kind"] == "state":
            self.antenna_state = rec["state"]
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
            self.antenna_state = None
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
                if gen == self._gen:
                    self.hub.antenna_state = None  # unknown until the next STATE line: no manual moves on a stale "Idle"
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


class HubWebPoller(threading.Thread):
    """Polls the TV-Hub's web service (read-only messages only: tvhub_webservice.READ_ONLY)
    and publishes the replies as "web" records. One request at a time, at most one a second;
    what is already known is republished only when it changes or every REPUBLISH_S."""

    EVERY_S = {"antenna_status": 5, "power": 60, "get_event_history_count": 60, "get_antenna_config": 600,
               "ophours": 600, "antenna_versions": 1800, "get_satellite_list": 1800, "get_autoswitch_status": 30}
    PARAMS_EVERY_S = 900        # get_satellite_params, for the tracked satellite and each favourite
    REPUBLISH_S = {"antenna_status": 60}  # at least this often even if unchanged
    REPUBLISH_DEFAULT_S = 1800
    RETRY_S = 30                # after the hub stops answering
    GAP_S = 1.0                 # between requests
    # antenna_status fields that change every poll; the rest decides whether it is news
    _VOLATILE = {"snr", "bars", "bst_az", "bst_el", "bst_tilt", "az_bow", "motor_az", "motor_el", "motor_skew", "heading"}

    def __init__(self, hub: Hub, host: str, port: int = webservice.WEB_PORT, fetch=webservice.fetch) -> None:
        super().__init__(name="tvhub-web", daemon=True)
        self.hub, self.host, self.port, self._fetch = hub, host, port, fetch
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._gen = 0
        self._reset()

    def _reset(self) -> None:
        self._due = {name: 0.0 for name in self.EVERY_S}
        self._params_due: dict[str, float] = {}
        self._last: dict[str, tuple[str, float]] = {}  # key -> (content, monotonic time published)
        self._events_wanted = False
        self._event_count: int | None = None
        self._pause_until = 0.0
        self._failing = False
        self._disabled: set[str] = set()
        self._last_err: dict[str, str] = {}  # message -> last error noted, so a repeating one is noted once
        self.group: dict | None = None  # latest get_autoswitch_status: the installed group, autoswitch on/off

    def retarget(self, host: str) -> None:
        with self._lock:
            self.host = host
            self._gen += 1
            self._reset()

    def stop(self) -> None:
        self._stop_evt.set()

    def installed_group(self) -> dict | None:
        with self._lock:
            return self.group

    def refresh_soon(self) -> None:
        """Ask for the hub's status and group again now, e.g. after a satellite change."""
        with self._lock:
            for name in ("antenna_status", "get_autoswitch_status"):
                self._due[name] = 0.0

    def _next_job(self, now: float):
        if now < self._pause_until:
            return None
        if self._events_wanted and self._event_count:
            return "get_recent_event_history", {"begin_at_event": 1, "how_many_events": min(self._event_count, 9999)}
        due = [(t, name, None) for name, t in self._due.items() if t <= now and name not in self._disabled]
        if "get_satellite_params" not in self._disabled:
            due += [(t, "get_satellite_params", {"antSatID": sat}) for sat, t in self._params_due.items() if t <= now]
        if not due:
            return None
        _, name, params = min(due, key=lambda d: d[0])
        return name, params

    def run(self) -> None:
        while not self._stop_evt.is_set():
            with self._lock:
                host, gen = self.host, self._gen
                job = self._next_job(time.monotonic())
            if job is None:
                self._stop_evt.wait(0.5)
                continue
            name, params = job
            try:
                data = self._fetch(host, name, params, 5.0, self.port)
            except webservice.WebServiceError as e:
                self._failed(gen, name, params, e)
            except Exception as e:  # noqa: BLE001 - a reply we could not parse must not kill the poller
                self._failed(gen, name, params, webservice.WebServiceError(f"{name}: unreadable reply ({e})"))
            else:
                self._answered(gen, name, params, data)
            self._stop_evt.wait(self.GAP_S)

    # These three run on the poller thread. They never call into the Hub while holding
    # self._lock: App.set_target holds the Hub's lock while it calls retarget().

    def _news(self, key: str, name: str, data) -> str | None:  # caller holds self._lock
        """The record text to publish, or None when nothing changed and it isn't due again."""
        content = json.dumps({"msg": name, "data": data}, separators=(",", ":"), sort_keys=True)
        compare = content
        if name == "antenna_status":
            compare = json.dumps({k: v for k, v in data.items() if k not in self._VOLATILE}, sort_keys=True)
        now = time.monotonic()
        last = self._last.get(key)
        if last and last[0] == compare and now - last[1] < self.REPUBLISH_S.get(name, self.REPUBLISH_DEFAULT_S):
            return None
        self._last[key] = (compare, now)
        return content

    def _emit(self, gen: int, content: str | None, notes: list[str]) -> None:
        guard = lambda: self._gen == gen  # noqa: E731 - checked under the Hub's lock
        for text in notes:
            self.hub.publish(time.time(), "bridge", text, guard=guard)
        if content:
            self.hub.publish(time.time(), "web", content, guard=guard)

    def _answered(self, gen: int, name: str, params, data: dict) -> None:
        now, notes, content = time.monotonic(), [], None
        with self._lock:
            if gen != self._gen:
                return
            if self._failing:
                self._failing = False
                notes.append(f"TV-Hub web service answering again ({self.host})")
            self._last_err.pop(name, None)
            if name == "get_satellite_params":
                self._params_due[params["antSatID"]] = now + self.PARAMS_EVERY_S
                key = "params:" + params["antSatID"]
            else:
                if name in self._due:
                    self._due[name] = now + self.EVERY_S[name]
                key = name
            if name == "antenna_status" and data.get("sat") and data["sat"] not in self._params_due:
                self._params_due[data["sat"]] = now  # the tracked satellite's settings, straight away
            if name == "get_autoswitch_status":
                self.group = data
            if name == "get_satellite_list":
                keep = [s for s in data["sats"] if s["id"] and (s["favorite"] or s["user"])]
                for s in keep:
                    self._params_due.setdefault(s["id"], now + 2)
                data = {"sats": keep}
            if name == "get_event_history_count":
                last = self._last.get("get_recent_event_history")
                stale = last is None or now - last[1] >= self.REPUBLISH_DEFAULT_S
                if data["count"] != self._event_count or stale:
                    self._events_wanted = bool(data["count"])
                    if data["count"] == 0:  # empty or just-cleared log: nothing to ask for, but tell the page
                        content = self._news("get_recent_event_history", "get_recent_event_history", {"events": []})
                self._event_count = data["count"]
            else:
                if name == "get_recent_event_history":
                    self._events_wanted = False
                content = self._news(key, name, data)
        self._emit(gen, content, notes)

    def _failed(self, gen: int, name: str, params, err: Exception) -> None:
        now, notes = time.monotonic(), []
        with self._lock:
            if gen != self._gen:
                return
            # Move this job back either way, so one message that keeps failing can't starve the rest.
            if name == "get_recent_event_history":
                self._events_wanted = False
                self._last["get_recent_event_history"] = ("", now)  # again when the count changes, or after a while
            elif name == "get_satellite_params":
                self._params_due[params["antSatID"]] = now + self.PARAMS_EVERY_S
            elif name in self._due:
                self._due[name] = now + max(self.EVERY_S[name], self.RETRY_S)
            if isinstance(err, webservice.HubUnreachable):
                self._pause_until = now + self.RETRY_S
                if not self._failing:
                    self._failing = True
                    notes.append(f"TV-Hub web service not answering ({err}); retrying every {self.RETRY_S} s")
            else:
                if self._failing:  # it answered, even if with an error
                    self._failing = False
                    notes.append(f"TV-Hub web service answering again ({self.host})")
                permanent = getattr(err, "code", None) in webservice.PERMANENT_ERRORS and name != "get_satellite_params"
                if permanent:
                    self._disabled.add(name)
                text = f"TV-Hub web service: {err}" + (" (not asking again this session)" if permanent else "")
                if self._last_err.get(name) != text:
                    self._last_err[name] = text
                    notes.append(text)
        self._emit(gen, None, notes)


class App:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.mode = "replay" if args.replay else "live"
        self.read_only = args.read_only or self.mode == "replay"
        self._cmd_lock = threading.Lock()
        self._target_lock = threading.Lock()
        self._last_cmd = 0.0
        self._select_lock = threading.Lock()
        self._last_select = -1e9
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
        # Read-only polling of the hub's web service; off with --read-only ("never send anything").
        self.web = None
        if self.mode == "live" and not self.read_only and not getattr(args, "no_hub_web", False):
            self.web = HubWebPoller(self.hub, args.host, port=getattr(args, "hub_web_port", None) or webservice.WEB_PORT)

    def start(self) -> None:
        self.source.start()
        if self.web:
            self.web.start()

    def hello(self, status: dict) -> dict:
        a = self.args
        info = {"session": self.hub.session, "mode": self.mode, "status": status,
                "commands": not self.read_only, "queries": list(QUERY_COMMANDS),
                "manual": {"jog": JOG_COMMANDS, "limits": MANUAL_LIMITS},
                "api": BRIDGE_API, "started": STARTED, "code_changed": _code_fingerprint() != CODE_FINGERPRINT,
                "hub_web": self.web is not None}
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
        if is_manual_move(command) and self.hub.antenna_state != "Idle":
            reason = f"manual pointing needs Idle mode (antenna is {self.hub.antenna_state or 'unknown'}): send HALT first"
            self.hub.note(f"refused to send {command!r}: {reason}")
            raise CommandRejected(reason, 409)
        with self._cmd_lock:
            now = time.monotonic()
            if now - self._last_cmd < MIN_COMMAND_INTERVAL_S:
                raise CommandRejected("commands are limited to two per second", 429)
            self.source.send(command)
            self._last_cmd = now
            self.hub.publish(time.time(), "tx", command)
        return command

    SELECT_INTERVAL_S = 10

    def select_satellite(self, sat) -> str:
        """Ask the TV-Hub to switch to another satellite of its installed group, as its own
        web page does (select_satellite, install=N). Only a satellite in the group the hub
        reported, only while autoswitch is off, and at most once every SELECT_INTERVAL_S."""
        if self.mode != "live":
            raise RequestRefused("replaying a log", 409)
        if self.read_only:
            raise CommandRejected("the bridge was started with --read-only", 409)
        if not self.web:
            raise RequestRefused("the TV-Hub web service is off (--no-hub-web)", 409)
        group = self.web.installed_group()
        if not group:
            raise RequestRefused("the TV-Hub's installed group isn't known yet; try again in a few seconds", 409)
        if group.get("enabled"):
            raise RequestRefused("autoswitch is on, so the receivers choose the satellite", 409)
        wanted = str(sat).strip().upper()
        member = next((s for s in group.get("sats", []) if (s.get("id") or "").upper() == wanted), None)
        if member is None:
            shown = re.sub(r"[^\x20-\x7e]", "?", str(sat).strip())[:20]
            ids = ", ".join(s["id"] for s in group.get("sats", []))
            self.hub.note(f"refused satellite change to {shown!r}: not in the installed group ({ids})")
            raise CommandRejected(f"{shown} is not in the installed group ({ids})")
        with self._select_lock:
            now = time.monotonic()
            if now - self._last_select < self.SELECT_INTERVAL_S:
                raise CommandRejected(f"one satellite change every {self.SELECT_INTERVAL_S} s", 429)
            self._last_select = now
        label = member["id"] + (f" ({member['name']})" if member.get("name") else "")
        self.hub.note(f"asking the TV-Hub to switch to {label}")
        try:
            webservice.select_satellite(self.args.host, member["id"], port=self.web.port)
        except webservice.WebServiceError as e:
            self.hub.note(f"TV-Hub did not switch to {member['id']}: {e}")
            raise RequestRefused(f"the TV-Hub did not accept it: {e}", 502) from e
        self.hub.note(f"TV-Hub accepted the switch to {label}")
        self.web.refresh_soon()
        return member["id"]

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

            def before() -> None:
                self.source.retarget(host, port)
                if self.web:
                    self.web.retarget(host)

            self.hub.new_session(before=before, hello=self.hello, note=f"dish address set to {host}:{port}")
            save_config(self.args.config, {**load_config(self.args.config), "host": host, "port": port})
        return host, port

    def reconnect(self) -> None:
        if self.mode != "live":
            raise RequestRefused("not in live mode", 409)
        self.source.reconnect()

    def close(self) -> None:
        self.source.stop()
        if self.web:
            self.web.stop()
        self.hub.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "tvhub-bridge/1"
    app: App  # set on the per-server subclass by make_server() / main()
    allowed_hosts: set[str] | None = None
    http_port = 0

    def log_request(self, code="-", size="-") -> None:
        pass

    def _host_ok(self) -> bool:
        # Loopback binding: only answer to loopback names (blocks DNS-rebinding pages).
        return lan.host_allowed(self.headers.get("Host", ""), self.allowed_hosts, self.http_port)

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
        if path not in ("/api/command", "/api/target", "/api/reconnect", "/api/satellite"):
            return self._json(404, {"error": "not found"})
        body = self._read_json()
        if body is None:
            return
        try:
            if path == "/api/command":
                return self._json(200, {"ok": True, "sent": self.app.send_command(str(body.get("cmd", "")))})
            if path == "/api/satellite":
                return self._json(200, {"ok": True, "sat": self.app.select_satellite(body.get("sat", ""))})
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


def _is_loopback(http_host: str) -> bool:
    try:
        return ipaddress.ip_address(http_host).is_loopback
    except ValueError:
        return http_host == "localhost"


def make_server(app: App | None, http_host: str, http_port: int, hosts: set[str] | None = None) -> ThreadingHTTPServer:
    """Bind the GUI server (port 0 = any free port). Set .app on the handler before serving.
    Requests must name an allowed Host (this is what defeats DNS rebinding): loopback names
    when bound to loopback, else `hosts` or this PC's own names and addresses."""
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer((http_host, http_port), handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    handler.http_port = port
    if _is_loopback(http_host):
        handler.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    else:
        handler.allowed_hosts = hosts if hosts is not None else lan.allowed_hosts(port, ips=lan.local_ipv4s())
    return httpd


def _bridge_answers(port: int) -> bool:
    """True if a monitor bridge is already serving on this port on this PC."""
    import http.client
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1.5)
        conn.request("GET", "/api/status", headers={"Host": f"127.0.0.1:{port}"})
        resp = conn.getresponse()
        data = json.loads(resp.read(65536) or b"{}") if resp.status == 200 else {}
        conn.close()
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and "session" in data and "mode" in data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TV-Hub port-50001 bridge and GUI server.")
    ap.add_argument("--host", help=f"dish (TV-Hub) address; default: the last one set in the GUI, else {DEFAULT_HOST}")
    ap.add_argument("--port", type=int, help=f"TV-Hub maintenance port (default {DEFAULT_PORT})")
    ap.add_argument("--replay", metavar="LOG", help="show a saved capture instead of connecting")
    ap.add_argument("--speed", type=float, default=0,
                    help="replay pacing: 0 = load instantly (default), 1 = real time, 10 = 10x")
    ap.add_argument("--read-only", action="store_true", help="never send anything to the TV-Hub")
    ap.add_argument("--eol", choices=sorted(EOLS), default="crlf", help="line ending for commands (default crlf)")
    ap.add_argument("--http-host", help="GUI bind address (default 127.0.0.1, or all interfaces with --lan)")
    ap.add_argument("--http-port", type=int, help="GUI port (default 8650, or 80 with --lan)")
    ap.add_argument("--lan", action="store_true",
                    help="serve the page to other devices on the local network too, as http://<name>.local/ "
                         "(remembered in the config file; --local turns it off again)")
    ap.add_argument("--local", action="store_true", help="this PC only, even if the config file says --lan")
    ap.add_argument("--name", help="the .local name announced with --lan (default kvh)")
    ap.add_argument("--log-dir", default=os.path.join(HERE, "logs"), help="where live sessions are logged")
    ap.add_argument("--no-log", action="store_true", help="don't write a session log")
    ap.add_argument("--history", type=int, default=50000,
                    help="records kept for browsers that connect later (default %(default)s, about 10 h)")
    ap.add_argument("--config", default=CONFIG_FILE, help="where the dish address set in the GUI is saved")
    ap.add_argument("--no-browser", action="store_true", help="don't open the GUI automatically")
    ap.add_argument("--no-hub-web", action="store_true",
                    help="don't poll the TV-Hub's web service (read-only status, power and satellite settings)")
    ap.add_argument("--hub-web-port", type=int, default=webservice.WEB_PORT, help=argparse.SUPPRESS)  # for testing
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

    # --lan / --local are remembered, so a double-clicked tvhub_server.py starts the same way
    if args.lan or args.local or args.name:
        save_config(args.config, {**load_config(args.config), "lan": bool(args.lan or (not args.local and saved.get("lan"))),
                                  **({"lan_name": args.name.lower()} if args.name else {})})
        saved = load_config(args.config)
    use_lan = not args.local and (args.lan or saved.get("lan") is True)
    name = (args.name or saved.get("lan_name") or "kvh").lower()
    if use_lan and not lan.valid_name(name):
        ap.error(f"not a valid host name: {name!r} (letters, digits and hyphens)")
    http_host = args.http_host or ("0.0.0.0" if use_lan else "127.0.0.1")
    ports = [args.http_port] if args.http_port else ([80, 8650] if use_lan else [8650])
    lan_ip = lan.lan_ipv4(args.host) if use_lan else None
    ips = (lan.local_ipv4s() | ({lan_ip} if lan_ip else set())) if use_lan else set()

    # Already running (a second double-click, say)? Open that one instead of starting another
    # bridge with a second connection to the dish.
    if not args.replay:
        for http_port in ports:
            if _bridge_answers(http_port):
                shown = f"http://localhost{'' if http_port == 80 else f':{http_port}'}/"
                print(f"The monitor is already running: opening {shown}")
                if not args.no_browser:
                    webbrowser.open(shown)
                time.sleep(4)  # long enough to read in a double-clicked window
                return 0

    httpd, errors = None, []
    for http_port in ports:
        try:
            hosts = lan.allowed_hosts(http_port, [name], ips) if not _is_loopback(http_host) else None
            httpd = make_server(None, http_host, http_port, hosts)
            break
        except OSError as e:
            errors.append(f"cannot listen on {http_host}:{http_port}: {e}")
    if httpd is None:
        print("\n".join(errors), file=sys.stderr)
        return 1
    for err in errors:
        print(err + " (using the next port)")
    http_port = httpd.server_address[1]
    app = App(args)
    httpd.RequestHandlerClass.app = app
    app.start()

    suffix = "" if http_port == 80 else f":{http_port}"
    url = f"http://localhost{suffix}/" if use_lan else f"http://127.0.0.1:{http_port}/"
    mdns = None
    if use_lan and lan_ip:
        try:
            mdns = lan.MdnsResponder(name, lan_ip)
            mdns.start()
        except OSError as e:
            print(f"could not announce {name}.local ({e}); use the addresses below instead")
            mdns = None
    if app.mode == "live":
        print(f"TV-Hub {args.host}:{args.port} -> {url}"
              + ("  (read-only)" if args.read_only else "  (allowlisted commands enabled)"))
        if app.hub.log_path:
            print(f"logging to {app.hub.log_path}")
    else:
        print(f"replaying {args.replay} -> {url}")
    if use_lan:
        pc = socket.gethostname().lower()
        others = ([f"http://{name}.local{suffix}/"] if mdns else []) + [f"http://{pc}.local{suffix}/"] + \
                 [f"http://{ip}{suffix}/" for ip in sorted(ips)]
        print("on other devices: " + "  ".join(others))
        print("anyone on this network can use the page, including its commands")
    print("Ctrl+C to stop")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if mdns:
            mdns.stop()
        app.close()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except SystemExit as e:  # argument errors
        code = e.code
    except Exception:  # noqa: BLE001 - show it rather than let a double-clicked window vanish
        import traceback
        traceback.print_exc()
        code = 1
    if code and os.name == "nt" and sys.stdin is not None and sys.stdin.isatty():
        try:
            input("\nPress Enter to close this window...")
        except (EOFError, KeyboardInterrupt):
            pass
    sys.exit(code)
