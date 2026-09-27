"""Serving the monitor to other devices on the local network.

- lan_ipv4(): this PC's address on the dish's network
- allowed_hosts(): the Host header values the bridge answers to. Checking Host is what
  stops a web page on some other site from reaching the bridge through DNS rebinding.
- MdnsResponder: answers multicast DNS queries for "<name>.local" (for example kvh.local)
  with this PC's address, so phones and other computers can open http://kvh.local/.
  Only A records for that one name; everything else is ignored. Stdlib only.
"""

from __future__ import annotations

import re
import socket
import struct
import threading

MDNS_GROUP, MDNS_PORT = "224.0.0.251", 5353
_RE_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def valid_name(name: str) -> bool:
    return bool(_RE_LABEL.match(name or ""))


def lan_ipv4(towards: str = "192.168.0.1") -> str | None:
    """The local address the OS would use to reach `towards` (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((towards, 9))
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip


def local_ipv4s() -> set[str]:
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return {ip for ip in ips if not ip.startswith("127.")}


def allowed_hosts(port: int, names=(), ips=()) -> set[str]:
    """Host header values to accept: loopback, this PC's name, the mDNS name, its addresses."""
    hosts = {"localhost", "127.0.0.1", "[::1]"}
    pc = socket.gethostname().lower()
    hosts |= {pc, pc + ".local"}
    fqdn = socket.getfqdn().lower()
    if fqdn and fqdn != "localhost":
        hosts.add(fqdn)
    hosts |= {n.lower() + ".local" for n in names if n}
    hosts |= set(ips)
    out = {f"{h}:{port}" for h in hosts}
    if port == 80:  # browsers leave the default port out of Host
        out |= hosts
    return out


def host_allowed(host: str, allowed: set[str] | None, port: int) -> bool:
    if allowed is None:
        return True
    h = (host or "").strip().lower()
    if h in allowed:
        return True
    name, sep, p = h.rpartition(":")
    if not sep or "]" in p:  # no port in the header
        name, p = h, ""
    if (p and p != str(port)) or (not p and port != 80):
        return False
    # *.localhost always means this machine: browsers resolve it to loopback themselves
    return name == "localhost" or name.endswith(".localhost")


# ---------------------------------------------------------------------------
# multicast DNS
# ---------------------------------------------------------------------------

def _read_name(data: bytes, off: int) -> tuple[str, int]:
    labels, end, hops = [], None, 0
    while True:
        if off >= len(data):
            raise ValueError("truncated name")
        n = data[off]
        if n == 0:
            off += 1
            break
        if n & 0xC0 == 0xC0:  # compression pointer
            if off + 1 >= len(data):
                raise ValueError("truncated pointer")
            if end is None:
                end = off + 2
            off = ((n & 0x3F) << 8) | data[off + 1]
            hops += 1
            if hops > 16:
                raise ValueError("pointer loop")
            continue
        if n & 0xC0:
            raise ValueError("bad label")
        labels.append(data[off + 1:off + 1 + n].decode("ascii", "replace"))
        off += 1 + n
    return ".".join(labels).lower(), (end if end is not None else off)


def _encode_name(name: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode("ascii") for p in name.split(".")) + b"\0"


def answer(packet: bytes, src_port: int, fqdn: str, ip: str) -> tuple[bytes, bool] | None:
    """The reply to one mDNS packet, as (bytes, unicast), or None if it doesn't ask for fqdn's A record."""
    if len(packet) < 12:
        return None
    qid, flags, qd = struct.unpack("!3H", packet[:6])
    if flags & 0x8000 or qd == 0 or qd > 32:  # a response, or nothing asked
        return None
    off, match, qu, question = 12, False, False, b""
    try:
        for _ in range(qd):
            start = off
            name, off = _read_name(packet, off)
            if off + 4 > len(packet):
                return None
            qtype, qclass = struct.unpack("!2H", packet[off:off + 4])
            off += 4
            if name == fqdn and qtype in (1, 255) and (qclass & 0x7FFF) in (1, 255):
                match, qu = True, bool(qclass & 0x8000)
                question = _encode_name(fqdn) + struct.pack("!2H", 1, 1)
    except ValueError:
        return None
    if not match:
        return None
    legacy = src_port != MDNS_PORT  # a plain DNS resolver asking the multicast address directly
    rr = _encode_name(fqdn) + struct.pack("!HHIH", 1, 1 if legacy else 0x8001, 10 if legacy else 120, 4) + socket.inet_aton(ip)
    if legacy:
        return struct.pack("!6H", qid, 0x8400, 1, 1, 0, 0) + question + rr, True
    return struct.pack("!6H", 0, 0x8400, 0, 1, 0, 0) + rr, qu


def announcement(fqdn: str, ip: str) -> bytes:
    return struct.pack("!6H", 0, 0x8400, 0, 1, 0, 0) + _encode_name(fqdn) + struct.pack("!HHIH", 1, 0x8001, 120, 4) + socket.inet_aton(ip)


class MdnsResponder(threading.Thread):
    """Answers "<name>.local" with `ip` on the local network."""

    def __init__(self, name: str, ip: str, port: int = MDNS_PORT, group: str = MDNS_GROUP) -> None:
        super().__init__(name="tvhub-mdns", daemon=True)
        if not valid_name(name):
            raise ValueError(f"not a valid host name: {name!r}")
        self.fqdn, self.ip, self.port, self.group = name.lower() + ".local", ip, port, group
        self._stop_evt = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("", port))  # OSError here: another program holds the port exclusively
        try:
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(group) + socket.inet_aton(ip))
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        except OSError:
            self.sock.close()
            raise
        self.sock.settimeout(1.0)

    def run(self) -> None:
        for _ in range(2):  # announce, as RFC 6762 asks, so caches pick the name up
            self._send(announcement(self.fqdn, self.ip), (self.group, self.port))
            if self._stop_evt.wait(1.0):
                return
        while not self._stop_evt.is_set():
            try:
                packet, src = self.sock.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError:
                if self._stop_evt.is_set():
                    return
                continue
            reply = answer(packet, src[1], self.fqdn, self.ip)
            if reply:
                data, unicast = reply
                self._send(data, src if unicast else (self.group, self.port))

    def _send(self, data: bytes, to) -> None:
        try:
            self.sock.sendto(data, to)
        except OSError:
            pass

    def stop(self) -> None:
        self._stop_evt.set()
        try:
            self.sock.close()
        except OSError:
            pass


if __name__ == "__main__":  # double-clicked: start the monitor with the page on the local network
    import os
    import runpy
    import sys
    server = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tvhub_server.py")
    sys.argv = [server, "--lan", *sys.argv[1:]]
    runpy.run_path(server, run_name="__main__")
