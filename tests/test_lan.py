import http.client
import os
import socket
import struct
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import tvhub_lan as lan  # noqa: E402
from tvhub_server import make_server  # noqa: E402


def query(name: str, qtype: int = 1, qu: bool = False, qid: int = 0) -> bytes:
    q = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"
    return struct.pack("!6H", qid, 0, 1, 0, 0, 0) + q + struct.pack("!2H", qtype, 0x8001 if qu else 1)


class HostCheckTests(unittest.TestCase):
    def test_lan_names(self):
        allowed = lan.allowed_hosts(80, ["kvh"], {"192.168.1.50"})
        for host in ("kvh.local", "KVH.local", "kvh.local:80", "192.168.1.50", "localhost", "127.0.0.1:80",
                     socket.gethostname().lower() + ".local", "kvh.localhost", "anything.localhost:80"):
            with self.subTest(host=host):
                self.assertTrue(lan.host_allowed(host, allowed, 80))
        for host in ("evil.example", "evil.example:80", "kvh.local:8650", "192.168.50.2", "", "kvh.local.evil.example",
                     "localhost.evil.example", "kvh.localhost:81"):
            with self.subTest(host=host):
                self.assertFalse(lan.host_allowed(host, allowed, 80))

    def test_other_port(self):
        allowed = lan.allowed_hosts(8650, ["kvh"], {"192.168.1.50"})
        self.assertTrue(lan.host_allowed("kvh.local:8650", allowed, 8650))
        self.assertTrue(lan.host_allowed("x.localhost:8650", allowed, 8650))
        self.assertFalse(lan.host_allowed("kvh.local", allowed, 8650))
        self.assertFalse(lan.host_allowed("x.localhost", allowed, 8650))

    def test_server_refuses_foreign_host(self):
        httpd = make_server(None, "127.0.0.1", 0)
        port = httpd.server_address[1]
        httpd.RequestHandlerClass.allowed_hosts = lan.allowed_hosts(port, ["kvh"], {"192.168.1.50"})
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        try:
            for host, status in ((f"kvh.local:{port}", 404), (f"evil.example:{port}", 403)):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", "/nothing-here", headers={"Host": host})
                self.assertEqual(conn.getresponse().status, status, host)
                conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()


class MdnsAnswerTests(unittest.TestCase):
    IP = "192.168.1.50"

    def test_multicast_answer(self):
        reply, unicast = lan.answer(query("kvh.local"), 5353, "kvh.local", self.IP)
        self.assertFalse(unicast)
        qid, flags, qd, an = struct.unpack("!4H", reply[:8])
        self.assertEqual((qid, flags, qd, an), (0, 0x8400, 0, 1))
        self.assertTrue(reply.endswith(socket.inet_aton(self.IP)))
        self.assertIn(b"\x03kvh\x05local\x00\x00\x01\x80\x01", reply)  # A, IN with the cache-flush bit

    def test_unicast_requested_and_legacy(self):
        self.assertTrue(lan.answer(query("KVH.local", qu=True), 5353, "kvh.local", self.IP)[1])
        reply, unicast = lan.answer(query("kvh.local", qid=0x1234), 50000, "kvh.local", self.IP)  # a plain resolver
        self.assertTrue(unicast)
        self.assertEqual(struct.unpack("!4H", reply[:8]), (0x1234, 0x8400, 1, 1))

    def test_ignored(self):
        for packet in (query("other.local"), query("kvh.local", qtype=28), b"", b"\x00" * 11,
                       struct.pack("!6H", 0, 0x8400, 1, 0, 0, 0) + query("kvh.local")[12:],  # a response, not a query
                       struct.pack("!6H", 0, 0, 1, 0, 0, 0) + b"\xc0\x0c" + b"\x00\x01\x00\x01"):  # pointer loop
            with self.subTest(packet=packet[:20]):
                self.assertIsNone(lan.answer(packet, 5353, "kvh.local", self.IP))

    def test_names(self):
        for good in ("kvh", "dish-monitor", "a1"):
            self.assertTrue(lan.valid_name(good))
        for bad in ("", "-kvh", "kvh-", "kvh.local", "k v", "x" * 64):
            self.assertFalse(lan.valid_name(bad))


if __name__ == "__main__":
    unittest.main()
