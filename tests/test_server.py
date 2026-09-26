import argparse
import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from tvhub_server import (App, CommandRejected, RequestRefused, make_server, validate_command,  # noqa: E402
                          validate_target)

SAMPLE_LOG = os.path.join(ROOT, "samples", "pol-switch.log")
FORBIDDEN = ["ZAP", "HALT", "CLEAREE", "@CLEAREE", "=CAL", "=CALAZ", "@SAVE", "=TV", "=TVMODE",
             "SATINSTALL,USER6I", "SATCK,166EN,F1", "SAT,166EN,V", "SAT,166EN,X,L", "SAT,166EN,V,L,X",
             "STATE\r\nZAP", "STATE ZAP", "HELP", "THRESHOLD", "", "   "]


def wait_for(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def make_args(**kw):
    base = dict(host="127.0.0.1", port=0, replay=None, speed=0, read_only=False, eol="crlf",
                http_host="127.0.0.1", http_port=0, log_dir=tempfile.gettempdir(), no_log=True,
                history=1000, no_browser=True,
                config=os.path.join(tempfile.mkdtemp(prefix="tvhub-test-"), "tvhub_config.json"))
    base.update(kw)
    return argparse.Namespace(**base)


class FakeTvHub:
    """Stands in for the TV-Hub: sends a few lines, records everything it receives."""

    def __init__(self, lines):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.received = b""
        self.conn = None
        threading.Thread(target=self._run, args=(lines,), daemon=True).start()

    def _run(self, lines):
        self.conn, _ = self.srv.accept()
        self.conn.sendall(b"".join(line.encode() + b"\r\n" for line in lines))
        while True:
            try:
                data = self.conn.recv(4096)
            except OSError:
                return
            if not data:
                return
            self.received += data

    def close(self):
        for s in (self.conn, self.srv):
            try:
                s.close()
            except (OSError, AttributeError):
                pass


class AllowlistTests(unittest.TestCase):
    def test_queries_allowed_case_insensitive(self):
        self.assertEqual(validate_command("state", set()), "STATE")
        self.assertEqual(validate_command(" @ver ", set()), "@VER")
        self.assertEqual(validate_command("=sernum", set()), "=SERNUM")

    def test_sat_needs_known_satellite(self):
        self.assertEqual(validate_command("sat,166en,v,l", {"166EN"}), "SAT,166EN,V,L")
        with self.assertRaises(CommandRejected):
            validate_command("SAT,166EN,V,L", set())

    def test_forbidden(self):
        for cmd in FORBIDDEN:
            with self.subTest(cmd=cmd), self.assertRaises(CommandRejected):
                validate_command(cmd, {"166EN", "USER6I"})


class TargetValidationTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(validate_target(" 192.168.50.214 ", "50001"), ("192.168.50.214", 50001))
        self.assertEqual(validate_target("tvhub.local", 50001), ("tvhub.local", 50001))
        self.assertEqual(validate_target("[fe80::1]", 23), ("fe80::1", 23))

    def test_invalid(self):
        for host, port in [("", 50001), ("192.168.50.2144", 50001), ("bad host", 50001), ("a;b", 50001),
                           ("192.168.50.214", 0), ("192.168.50.214", 70000), ("192.168.50.214", "x")]:
            with self.subTest(host=host, port=port), self.assertRaises(RequestRefused):
                validate_target(host, port)


class LiveBridgeTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTvHub(["TV-HUB Maintenance port connection",
                               "+POS:  17.7  55.8 -22.8 3406 11.83 166EN TRACKSAT",
                               "+BST:  17.5  54.8  -5.0"])
        self.app = App(make_args(port=self.fake.port))
        self.app.source.start()
        self.assertTrue(wait_for(lambda: "166EN" in self.app.hub.known_sats), "no telemetry from fake hub")

    def tearDown(self):
        self.app.close()
        self.fake.close()

    def test_only_allowlisted_bytes_reach_the_socket(self):
        self.assertEqual(self.app.send_command("state"), "STATE")
        for cmd in FORBIDDEN + ["SAT,USER6I,V,H"]:  # USER6I never appeared in this session
            with self.subTest(cmd=cmd), self.assertRaises(CommandRejected):
                self.app.send_command(cmd)
        time.sleep(0.55)
        self.assertEqual(self.app.send_command("SAT,166EN,V,L"), "SAT,166EN,V,L")
        self.assertTrue(wait_for(lambda: self.fake.received == b"STATE\r\nSAT,166EN,V,L\r\n"),
                        repr(self.fake.received))

    def test_rate_limit(self):
        self.app.send_command("STATE")
        with self.assertRaises(CommandRejected) as ctx:
            self.app.send_command("STATE")
        self.assertEqual(ctx.exception.status, 429)

    def test_read_only(self):
        self.app.read_only = True
        with self.assertRaises(CommandRejected):
            self.app.send_command("STATE")
        time.sleep(0.2)
        self.assertEqual(self.fake.received, b"")

    def test_retarget_starts_clean_session(self):
        other = FakeTvHub(["TV-HUB Maintenance port connection",
                           "+POS:  17.7  57.6 -5.1 3000 11.00 USER6I TRACKSAT"])
        try:
            old_session = self.app.hub.session
            self.assertEqual(self.app.set_target("127.0.0.1", other.port), ("127.0.0.1", other.port))
            self.assertNotEqual(self.app.hub.session, old_session)
            self.assertTrue(wait_for(lambda: "USER6I" in self.app.hub.known_sats), "no telemetry from new dish")
            self.assertNotIn("166EN", self.app.hub.known_sats)  # SAT commands for the old dish no longer allowed
            with open(self.app.args.config, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), {"host": "127.0.0.1", "port": other.port})
            self.assertEqual(self.app.send_command("STATE"), "STATE")
            self.assertTrue(wait_for(lambda: other.received == b"STATE\r\n"), repr(other.received))
            self.assertEqual(self.fake.received, b"")  # nothing went to the old dish
        finally:
            other.close()

    def test_bad_target_refused(self):
        with self.assertRaises(RequestRefused) as ctx:
            self.app.set_target("not a host", 50001)
        self.assertEqual(ctx.exception.status, 400)


@unittest.skipUnless(os.path.exists(SAMPLE_LOG), "sample capture not present")
class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = App(make_args(replay=SAMPLE_LOG))
        cls.httpd = make_server(cls.app, "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        cls.app.source.start()
        cls.app.source.join(10)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.app.close()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp, resp.read()

    def test_gui_served(self):
        resp, body = self.request("GET", "/")
        self.assertEqual(resp.status, 200)
        self.assertIn(b"<html", body[:200].lower())

    def test_sse_hello_and_backlog(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/events")
        resp = conn.getresponse()
        self.assertEqual(resp.getheader("Content-Type").split(";")[0], "text/event-stream")
        events, name, records = [], None, []
        while True:
            line = resp.fp.readline().decode().rstrip("\n")
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                events.append(name)
                if name == "backlog":
                    records += json.loads(line[6:])
                if name == "ready":
                    break
                name = None
        conn.close()
        self.assertEqual(events[0], "hello")
        self.assertEqual(len(records), 496)
        self.assertEqual(records[1]["kind"], "pos")

    def test_command_refused_in_replay(self):
        resp, body = self.request("POST", "/api/command", json.dumps({"cmd": "STATE"}),
                                  {"Content-Type": "application/json"})
        self.assertEqual(resp.status, 409)

    def test_target_refused_in_replay(self):
        resp, body = self.request("POST", "/api/target", json.dumps({"host": "192.168.50.214", "port": 50001}),
                                  {"Content-Type": "application/json"})
        self.assertEqual(resp.status, 409)
        resp, body = self.request("POST", "/api/target", "[1, 2]", {"Content-Type": "application/json"})
        self.assertEqual(resp.status, 400)

    def test_csrf_guards(self):
        resp, _ = self.request("POST", "/api/command", json.dumps({"cmd": "STATE"}),
                               {"Content-Type": "text/plain"})
        self.assertEqual(resp.status, 415)
        resp, _ = self.request("POST", "/api/command", json.dumps({"cmd": "STATE"}),
                               {"Content-Type": "application/json", "Origin": "http://evil.example"})
        self.assertEqual(resp.status, 403)
        resp, _ = self.request("GET", "/api/status", headers={"Host": "evil.example:8650"})
        self.assertEqual(resp.status, 403)


if __name__ == "__main__":
    unittest.main()
