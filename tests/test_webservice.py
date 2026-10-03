import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import tvhub_webservice as ws  # noqa: E402
from tvhub_parser import format_log_line, make_record  # noqa: E402
from tvhub_server import Hub, HubWebPoller  # noqa: E402

# Replies shaped like the real TV-Hub's, with dummy values.
REPLIES = {
    "antenna_status": """<ipacu_response><message name="antenna_status" error="0" />
      <gps><lat>-27.4167</lat><lon>153.2500</lon><state>ACQUIRED</state><source>Antenna</source></gps>
      <acu><state>OK</state>
        <led_power><color>GREEN</color><state>ON</state><message>Valid input voltage applied to the TV HUB.</message></led_power>
        <led_antenna><color>GREEN</color><state>ON</state><message>Antenna is tracking selected satellite.</message></led_antenna>
        <led_acu><color>GREEN</color><state>ON</state><message>TV HUB has completed initialization.</message></led_acu>
        <line1/><line2/></acu>
      <antenna><state>TRACKING</state><rf><snr>{snr}</snr><bars>3</bars></rf>
        <brst><hdg/><az_bow>017.6</az_bow><az>017.6</az><el>53.9</el><tilt>-6.3</tilt><azoff/></brst>
        <motor><az>17.6</az><el>55.8</el><skew>-22.8</skew></motor></antenna>
      <satellite><antSatID>166EN</antSatID><name>Intelsat 19 NZ</name><region>Australia</region><lon>165.98</lon>
        <selected>166EN</selected></satellite></ipacu_response>""",
    "power": """<ipacu_response><message name="power" error="0" /><acu><inputsupplyv>30.1</inputsupplyv>
      <input42v>42.9</input42v><input24v/><eight>8.11</eight><five>5.01</five><three_three>3.35</three_three>
      <output42v>42.9</output42v><output24v>0.0</output24v><temp_celsius>37.6</temp_celsius></acu>
      <au><dc>40.3</dc><motor>32.4</motor><eight>8.1 </eight><five>5.0 </five><lnb>18.1</lnb>
      <temp_celsius>63.8</temp_celsius></au></ipacu_response>""",
    "antenna_versions": """<ipacu_response><message name="antenna_versions" error="0" /><ver_sync>Y</ver_sync>
      <acu><ver>2040</ver><sn>000000000</sn></acu><au><model>TV6</model><ver>2.50</ver><sn>000000000</sn></au>
      <gprs><ip>10.0.0.1</ip></gprs><sat_list><ver>8.4</ver></sat_list>
      <lnb><name>Aust Dual Linear</name><LO1_freq>10700</LO1_freq><LO1_tone>ON</LO1_tone><LO2_freq>OFF</LO2_freq>
      <LO2_tone>OFF</LO2_tone></lnb></ipacu_response>""",
    "get_antenna_config": """<ipacu_response><message name="get_antenna_config" error="0" />
      <sidelobe>OFF</sidelobe><sleep>ON</sleep></ipacu_response>""",
    "ophours": """<ipacu_response><message name="ophours" error="3" /></ipacu_response>""",  # answered with an error
    "get_satellite_list": """<ipacu_response><message name="get_satellite_list" error="0" /><satellite_list>
      <satellite><antSatID>166EN</antSatID><name>Intelsat 19 NZ</name><lon>165.98</lon><favorite>TRUE</favorite></satellite>
      <satellite><antSatID>75E</antSatID><name>ABS 1 North</name><lon>75.02</lon><favorite>FALSE</favorite></satellite>
      <satellite><antSatID>USER4</antSatID><name>Measat 3a</name><lon>160.00</lon><favorite>TRUE</favorite></satellite>
      </satellite_list></ipacu_response>""",
    "get_satellite_params": """<ipacu_response><message name="get_satellite_params" error="0" />
      <antSatID>{sat}</antSatID><name>Sat {sat}</name><lon>160.00</lon><skew>-45.0</skew><computedSkew>32.4</computedSkew>
      <lo1>10600</lo1><lo2>9750</lo2>
      <xponder><id>2</id><display>Vertical High</display><pol>V</pol><band>H</band><freq>00000</freq><symRate>20000</symRate>
        <fec>1/2</fec><netID>0XFFFE</netID><modType>QDVB</modType></xponder>
      <xponder><id>1</id><display>Horizontal High</display><pol>H</pol><band>H</band><freq>12279</freq><symRate>30000</symRate>
        <fec>3/4</fec><netID>0XFFFE</netID><modType>LQPSK</modType></xponder></ipacu_response>""",
    "get_lnb_list": """<ipacu_response><message name="get_lnb_list" error="0"></message><lnb_list>
      <name>19-0444 Single Linear</name><name>19-0298 Dual Linear</name><name>19-AUST Aust Dual Linear</name>
      </lnb_list><enable>N</enable></ipacu_response>""",
    "get_gps": """<ipacu_response><message name="get_gps" error="0"/><state>ACQUIRED</state>
      <lat>12.345678</lat><lon>-98.765432</lon><city>PLACEHOLDER CITY</city></ipacu_response>""",
    "get_heading_config": """<ipacu_response><message name="get_heading_config" error="0" />
      <nmea0183><enable>Y</enable><message_list>
        <nmea_message><nmea_name>True heading from north seeking gyro</nmea_name><heading_value>274</heading_value>
          <nmea_source>HEHDT</nmea_source><state>ACTIVE</state><selected>Y</selected></nmea_message>
        </message_list></nmea0183>
      <nmea2000><enable>N</enable><message_list>
        <nmea_message><nmea_name>Heading from magnetic compass</nmea_name><heading_value>260</heading_value>
          <nmea_source>MAG-HEADING</nmea_source><state>ACTIVE</state><selected>N</selected></nmea_message>
        </message_list></nmea2000></ipacu_response>""",
    "get_blockage_zones": """<ipacu_response><message error="0" name="get_blockage_zones"/>
      <el_support>TRUE</el_support><total_zones>2</total_zones><zone_list>
        <zone><id>1</id><state>ON</state><az_min>0</az_min><az_max>360</az_max><el_min>-5</el_min><el_max>10</el_max></zone>
        <zone><id>2</id><state>OFF</state><az_min>233</az_min><az_max>243</az_max><el_min>0</el_min><el_max>75</el_max></zone>
        </zone_list></ipacu_response>""",
    "get_hazard_zones": """<ipacu_response><message error="0" name="get_hazard_zones"/>
      <el_support>TRUE</el_support><mismatch>NO</mismatch><total_zones>1</total_zones>
      <acu_list><override>OFF</override>
        <zone><id>1</id><state>ON</state><az_min>100</az_min><az_max>120</az_max><el_min>0</el_min><el_max>30</el_max></zone>
        </acu_list>
      <ant_list><override>OFF</override>
        <zone><id>1</id><state>ON</state><az_min>100</az_min><az_max>120</az_max><el_min>0</el_min><el_max>30</el_max></zone>
        </ant_list></ipacu_response>""",
    "get_autoswitch_status": """<ipacu_response><message name="get_autoswitch_status" error="0" /><available>Y</available>
      <enable>N</enable><service>DISEQC</service><master><sn>000000000</sn><name>TV-Hub</name><valid>Y</valid><sat>B</sat></master>
      <satellite_group>Australia</satellite_group><satellites>
      <A><antSatID>USER6I</antSatID><name>Optus D3/10 Vas</name><lon>156.00</lon></A>
      <B><antSatID>166EN</antSatID><name>Intelsat 19 NZ</name><lon>165.98</lon></B>
      <C/><D><antSatID>USER4</antSatID><name>Measat 3a</name><lon>160.00</lon></D></satellites></ipacu_response>""",
    "select_satellite": """<ipacu_response><message name="select_satellite" error="0" /></ipacu_response>""",
    "get_event_history_count": """<ipacu_response><message name="get_event_history_count" error="0" />
      <event_count>2</event_count></ipacu_response>""",
    "get_recent_event_history": """<ipacu_response><message name="get_recent_event_history" error="0" /><event_list>
      <event>Sep 26 2026 10:05:31    ALERT::  Polarization and Band selection failure Measat 3a,H,L &lt;H/L&gt; is invalid</event>
      <event>Sep 26 2026 10:22:15 ALERT RESOLVED::  Polarization and Band selection failure Measat 3a,H,L &lt;H/L&gt; is invalid</event>
      </event_list></ipacu_response>""",
}

# What a TV-Hub that doesn't implement a message actually returns: malformed XML naming its
# generic unknown_xml_message handler, followed by a wrapper saying the server returned invalid XML.
UNKNOWN_REPLY = ('<?xml version="1.0" encoding="UTF-8"?><ipacu_response> '
                 '<message name="unknown_xml_message" error="1"</ipacu_response>\n'
                 '<?xml version="1.0"?>\n'
                 '<ipacu_response><message name="get_blockage_zones" error="server returned invalid xml"/></ipacu_response>')


class FakeWebService:
    """Stands in for the TV-Hub's /webservice.php: canned replies, records every request body."""

    def __init__(self):
        self.requests = []
        self.snr = 10.25
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                fake.requests.append((self.path, body))
                name = body.split('name="', 1)[1].split('"', 1)[0] if 'name="' in body else ""
                sat = body.split("<antSatID>", 1)[1].split("<", 1)[0] if "<antSatID>" in body else ""
                reply = REPLIES.get(name, '<ipacu_response><message name="x" error="3" /></ipacu_response>')
                reply = reply.replace("{snr}", str(fake.snr)).replace("{sat}", sat).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/xml")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def names(self):
        return [b.split('name="', 1)[1].split('"', 1)[0] for _, b in self.requests]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FastPoller(HubWebPoller):
    GAP_S = 0.02
    RETRY_S = 0.5


def wait_for(pred, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class RequestTests(unittest.TestCase):
    def test_only_read_only_messages(self):
        for name in ("set_antenna_config", "reboot", "reset_software", "install_software", "clear_event_history",
                     "get_product_registration", "get_wlan", "get_eth", "get_serial_log", "select_satellite",
                     "set_satellite_params", "start_serial_log", "set_date_time", "", "antenna_status "):
            with self.subTest(name=name), self.assertRaises(ws.WebServiceError):
                ws.build_request(name)

    def test_parameters_checked(self):
        self.assertEqual(ws.build_request("get_satellite_params", {"antSatID": "USER4"}),
                         b'<ipacu_request><message name="get_satellite_params" /><antSatID>USER4</antSatID></ipacu_request>')
        for name, params in [("get_satellite_params", {"antSatID": "USER4</antSatID><x>"}),
                             ("get_satellite_params", {"antSatID": ""}), ("antenna_status", {"antSatID": "USER4"}),
                             ("get_recent_event_history", {"begin_at_event": "0"}),
                             ("get_recent_event_history", {"how_many_events": "-1"})]:
            with self.subTest(params=params), self.assertRaises(ws.WebServiceError):
                ws.build_request(name, params)

    def test_error_reply(self):
        with self.assertRaises(ws.WebServiceError) as ctx:
            ws.check_reply("ophours", REPLIES["ophours"].encode())
        self.assertIn("unknown message", str(ctx.exception))
        with self.assertRaises(ws.WebServiceError):
            ws.check_reply("power", b"<html>not xml")

    def test_unknown_message_reply_is_permanent(self):
        # the real hub's malformed "unknown message" reply -> code 3 so the poller stops asking
        with self.assertRaises(ws.WebServiceError) as ctx:
            ws.check_reply("get_blockage_zones", UNKNOWN_REPLY.encode())
        self.assertEqual(ctx.exception.code, "3")
        self.assertIn("unknown message", str(ctx.exception))
        self.assertIn("3", ws.PERMANENT_ERRORS)
        # also the well-formed variant
        wf = b'<ipacu_response><message name="unknown_xml_message" error="1"/></ipacu_response>'
        with self.assertRaises(ws.WebServiceError) as ctx2:
            ws.check_reply("get_hazard_zones", wf)
        self.assertEqual(ctx2.exception.code, "3")
        # a genuinely corrupt reply (no unknown_xml_message) stays a non-permanent parse error
        with self.assertRaises(ws.WebServiceError) as ctx3:
            ws.check_reply("power", b"<ipacu_response><garbage")
        self.assertIsNone(ctx3.exception.code)


class SelectRequestTests(unittest.TestCase):
    def test_select_only_when_asked_for_and_never_a_reinstall(self):
        good = {"antSatID": "USER4", "install": "N"}
        self.assertEqual(ws.build_request("select_satellite", good, allow_select=True),
                         b'<ipacu_request><message name="select_satellite" /><antSatID>USER4</antSatID><install>N</install></ipacu_request>')
        with self.assertRaises(ws.WebServiceError):
            ws.build_request("select_satellite", good)  # not through the read-only path
        for params in ({"antSatID": "USER4", "install": "Y"}, {"antSatID": "USER4"}, {"install": "N"},
                       {"antSatID": "USER4", "install": "N", "extra": "1"}, {"antSatID": "US ER4", "install": "N"}):
            with self.subTest(params=params), self.assertRaises(ws.WebServiceError):
                ws.build_request("select_satellite", params, allow_select=True)
        with self.assertRaises(ws.WebServiceError):
            ws.build_request("set_antenna_config", {}, allow_select=True)

    def test_parse_autoswitch_leaves_out_serials(self):
        d = ws.parse_autoswitch(ws.check_reply("get_autoswitch_status", REPLIES["get_autoswitch_status"].encode()))
        self.assertEqual([x["id"] for x in d["sats"]], ["USER6I", "166EN", "USER4"])  # empty slot C skipped
        self.assertEqual((d["enabled"], d["group"], d["master"]["slot"]), (False, "Australia", "B"))
        self.assertNotIn("000000000", json.dumps(d))


class SelectSatelliteTests(unittest.TestCase):
    """App.select_satellite against a fake web service; the TV-Hub TCP link is never started."""

    def setUp(self):
        import argparse
        import tempfile
        from tvhub_server import App
        self.fake = FakeWebService()
        args = argparse.Namespace(host="127.0.0.1", port=50001, replay=None, speed=0, read_only=False, eol="crlf",
                                  http_host="127.0.0.1", http_port=0, log_dir=tempfile.gettempdir(), no_log=True,
                                  history=1000, no_browser=True, no_hub_web=False, hub_web_port=self.fake.port,
                                  config=os.path.join(tempfile.mkdtemp(prefix="tvhub-test-"), "tvhub_config.json"))
        self.app = App(args)
        self.group = ws.parse_autoswitch(ws.check_reply("get_autoswitch_status", REPLIES["get_autoswitch_status"].encode()))

    def tearDown(self):
        self.app.close()
        self.fake.close()

    def give_group(self, **changes):
        self.app.web._answered(self.app.web._gen, "get_autoswitch_status", None, dict(self.group, **changes))

    def test_switches_within_the_group(self):
        from tvhub_server import CommandRejected, RequestRefused
        with self.assertRaises(RequestRefused):
            self.app.select_satellite("USER4")  # group not known yet
        self.give_group()
        self.assertEqual(self.app.select_satellite(" user4 "), "USER4")
        self.assertEqual(self.fake.requests[-1][1],
                         '<ipacu_request><message name="select_satellite" /><antSatID>USER4</antSatID><install>N</install></ipacu_request>')
        with self.assertRaises(CommandRejected) as ctx:
            self.app.select_satellite("166EN")  # within 10 s of the last change
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(self.fake.names(), ["select_satellite"])

    def test_refusals_send_nothing(self):
        from tvhub_server import CommandRejected, RequestRefused
        self.give_group()
        for sat in ("USER2", "75E", "USER4</antSatID>", "", "166EN,USER4"):  # USER2 is not in this group
            with self.subTest(sat=sat), self.assertRaises(CommandRejected):
                self.app.select_satellite(sat)
        self.give_group(enabled=True)
        with self.assertRaises(RequestRefused):
            self.app.select_satellite("USER4")  # autoswitch on: the receivers choose
        self.app.read_only = True
        with self.assertRaises(CommandRejected):
            self.app.select_satellite("USER4")
        self.assertEqual(self.fake.requests, [])

    def test_hub_error_reported(self):
        from tvhub_server import RequestRefused
        self.give_group()
        REPLIES["select_satellite"] = REPLIES["select_satellite"].replace('error="0"', 'error="5"')
        try:
            with self.assertRaises(RequestRefused) as ctx:
                self.app.select_satellite("USER4")
            self.assertEqual(ctx.exception.status, 502)
        finally:
            REPLIES["select_satellite"] = REPLIES["select_satellite"].replace('error="5"', 'error="0"')


class ParseTests(unittest.TestCase):
    def reply(self, name, **fill):
        text = REPLIES[name]
        for k, v in fill.items():
            text = text.replace("{" + k + "}", v)
        return ws.check_reply(name, text.encode())

    def test_antenna_status_leaves_out_position(self):
        d = ws.parse_antenna_status(self.reply("antenna_status", snr="10.25"))
        self.assertEqual(d["leds"]["antenna"]["color"], "GREEN")
        self.assertEqual((d["state"], d["sat"], d["snr"], d["bars"]), ("TRACKING", "166EN", 10.25, 3.0))
        self.assertNotIn("-27.4167", json.dumps(d))

    def test_power_and_versions(self):
        p = ws.parse_power(self.reply("power"))
        self.assertEqual((p["hub"]["output42v"], p["antenna"]["dc"], p["antenna"]["lnb"], p["hub"]["input24v"]), (42.9, 40.3, 18.1, None))
        v = ws.parse_versions(self.reply("antenna_versions"))
        self.assertEqual((v["sat_library"], v["lnb"]["LO2_freq"]), ("8.4", "OFF"))
        self.assertNotIn("10.0.0.1", json.dumps(v))  # modem address and serials are left out
        self.assertNotIn("000000000", json.dumps(v))

    def test_satellite_params_sorted(self):
        d = ws.parse_satellite_params(self.reply("get_satellite_params", sat="USER4"))
        self.assertEqual([x["id"] for x in d["xponders"]], ["1", "2"])
        self.assertEqual((d["skew_offset"], d["computed_skew"]), (-45.0, 32.4))

    def test_lnb_list(self):
        d = ws.parse_lnb_list(self.reply("get_lnb_list"))
        self.assertEqual(d["lnbs"][-1], "19-AUST Aust Dual Linear")
        self.assertEqual(len(d["lnbs"]), 3)
        self.assertFalse(d["custom_enabled"])

    def test_gps_keeps_position_out(self):
        d = ws.parse_gps(self.reply("get_gps"))
        self.assertEqual(d["state"], "ACQUIRED")
        blob = json.dumps(d)  # the position must never reach the record stream or the session log
        for leak in ("12.345678", "98.765432", "PLACEHOLDER CITY", "lat", "lon", "city"):
            self.assertNotIn(leak, blob)

    def test_heading_config(self):
        d = ws.parse_heading_config(self.reply("get_heading_config"))
        self.assertTrue(d["has_heading"])
        self.assertEqual((d["selected"]["source"], d["selected"]["heading"], d["selected"]["bus"]), ("HEHDT", 274.0, "nmea0183"))
        self.assertEqual(len(d["sources"]), 2)  # both buses' messages are listed; only the active, selected one is chosen

    def test_blockage_zones(self):
        d = ws.parse_blockage_zones(self.reply("get_blockage_zones"))
        self.assertEqual((d["el_support"], d["total"], len(d["zones"])), (True, 2, 2))
        z = d["zones"][0]
        self.assertEqual((z["id"], z["state"], z["az_min"], z["az_max"], z["el_min"], z["el_max"]), ("1", "ON", 0.0, 360.0, -5.0, 10.0))

    def test_hazard_zones(self):
        d = ws.parse_hazard_zones(self.reply("get_hazard_zones"))
        self.assertFalse(d["mismatch"])
        self.assertEqual((d["acu"]["override"], len(d["acu"]["zones"])), (False, 1))
        self.assertEqual(d["ant"]["zones"][0]["az_min"], 100.0)

    def test_events_in_utc(self):
        ev = ws.parse_events(self.reply("get_recent_event_history"))["events"]
        self.assertEqual(ev[0]["level"], "ALERT")
        self.assertEqual(ev[1]["level"], "ALERT RESOLVED")
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ev[0]["t"])), "2026-09-26 10:05:31")
        self.assertTrue(ev[0]["text"].endswith("<H/L> is invalid"))

    def test_web_record_and_log_line(self):
        rec = make_record('{"msg":"power","data":{"hub":{"five":5.01}}}', 1.0, "web", 7)
        self.assertEqual((rec["kind"], rec["msg"], rec["data"]["hub"]["five"]), ("hubweb", "power", 5.01))
        self.assertEqual(make_record("not json", 1.0, "web")["kind"], "unparsed")
        self.assertIn("\tWB\t", format_log_line(1.79e9, "web", "{}"))


class PollerTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeWebService()
        self.hub = Hub(1000, None)
        self.poller = FastPoller(self.hub, "127.0.0.1", port=self.fake.port)

    def tearDown(self):
        self.poller.stop()
        if self.poller.is_alive():
            self.poller.join(3)
        self.fake.close()

    def records(self, kind=None):
        client, backlog, _, _ = self.hub.subscribe()
        self.hub.unsubscribe(client)
        recs = [json.loads(b) for b in backlog]
        return [r for r in recs if kind is None or r["kind"] == kind]

    def web(self, msg):
        return [r for r in self.records("hubweb") if r["msg"] == msg]

    def test_polls_only_read_only_messages_and_publishes(self):
        self.poller.start()
        self.assertTrue(wait_for(lambda: self.web("get_recent_event_history") and self.web("get_satellite_params")
                                 and len({r["data"]["sat"] for r in self.web("get_satellite_params")}) >= 2),
                        self.fake.names())
        self.assertTrue(set(self.fake.names()) <= set(ws.READ_ONLY), self.fake.names())
        self.assertTrue(all(path == "/webservice.php" for path, _ in self.fake.requests))
        sats = {r["data"]["sat"] for r in self.web("get_satellite_params")}
        self.assertEqual(sats, {"166EN", "USER4"})  # the tracked one and the favourites, not every library satellite
        lst = self.web("get_satellite_list")[0]["data"]["sats"]
        self.assertEqual({s["id"] for s in lst}, {"166EN", "USER4"})
        # ophours answered with an error: reported once, then not asked again
        notes = [r["text"] for r in self.records("bridge")]
        self.assertTrue(any("ophours" in n and "unknown message" in n for n in notes), notes)
        self.assertEqual(self.fake.names().count("ophours"), 1)

    def test_antenna_status_published_on_change_only(self):
        self.poller.EVERY_S = dict(HubWebPoller.EVERY_S, antenna_status=0.05)
        self.poller.start()
        self.assertTrue(wait_for(lambda: self.fake.names().count("antenna_status") >= 4))
        self.assertEqual(len(self.web("antenna_status")), 1)  # only the SNR moved: not news
        self.fake.snr = 3.5
        time.sleep(0.3)
        self.assertEqual(len(self.web("antenna_status")), 1)
        REPLIES["antenna_status"] = REPLIES["antenna_status"].replace("<state>TRACKING</state>", "<state>SEARCHING</state>")
        try:
            self.assertTrue(wait_for(lambda: len(self.web("antenna_status")) == 2))
            self.assertEqual(self.web("antenna_status")[-1]["data"]["state"], "SEARCHING")
        finally:
            REPLIES["antenna_status"] = REPLIES["antenna_status"].replace("<state>SEARCHING</state>", "<state>TRACKING</state>")

    def test_unknown_message_stops_being_asked(self):
        orig = REPLIES["get_blockage_zones"]
        REPLIES["get_blockage_zones"] = UNKNOWN_REPLY  # the hub says it doesn't implement it
        self.poller.EVERY_S = dict(HubWebPoller.EVERY_S, get_blockage_zones=0.05, antenna_status=0.05)
        try:
            self.poller.start()
            self.assertTrue(wait_for(lambda: self.fake.names().count("antenna_status") >= 6))  # many cycles pass
            self.assertEqual(self.fake.names().count("get_blockage_zones"), 1)  # asked once, then disabled
            notes = [r["text"] for r in self.records("bridge")]
            self.assertTrue(any("not asking again" in n and "blockage" in n.lower() for n in notes), notes)
        finally:
            REPLIES["get_blockage_zones"] = orig

    def test_unreachable_hub_noted_once(self):
        calls = []

        def unreachable(host, name, *a):
            calls.append(name)
            raise ws.HubUnreachable(f"{name}: refused")

        self.poller.RETRY_S = 0.05
        self.poller._fetch = unreachable
        self.poller.start()
        self.assertTrue(wait_for(lambda: len(calls) >= 4))
        self.assertEqual(sum("not answering" in r["text"] for r in self.records("bridge")), 1)
        self.assertGreater(len(set(calls)), 1)  # a failed message goes to the back: the others still get asked

    def test_one_failing_message_does_not_starve_the_rest(self):
        real, calls = self.poller._fetch, []

        def fetch(host, name, params, timeout, port):
            calls.append(name)
            if name == "get_satellite_params":
                raise ws.HubUnreachable(f"{name}: HTTP 500")
            if name == "power":
                err = ws.WebServiceError("power: hub answered error 14 (datastore unavailable)")
                err.code = "14"
                raise err
            return real(host, name, params, timeout, port)

        self.poller.RETRY_S = 0.05
        self.poller.EVERY_S = dict(HubWebPoller.EVERY_S, antenna_status=0.05, power=0.05)
        self.poller._fetch = fetch
        self.poller.start()
        self.assertTrue(wait_for(lambda: calls.count("antenna_status") >= 5 and calls.count("power") >= 3), calls)
        notes = [r["text"] for r in self.records("bridge")]
        self.assertEqual(sum("error 14" in n for n in notes), 1, notes)  # a transient error: retried, noted once
        self.assertFalse(any("not asking again" in n and "power" in n for n in notes), notes)

    def test_reply_in_flight_for_old_hub_dropped(self):
        started, release, hosts = threading.Event(), threading.Event(), []

        def fetch(host, name, params, timeout, port):
            hosts.append(host)
            if len(hosts) == 1:  # the old hub's answer arrives only after the retarget
                started.set()
                release.wait(5)
                raise ws.HubUnreachable(f"{name}: old hub")
            return ws.parse_antenna_status(ws.check_reply(name, REPLIES["antenna_status"].replace("{snr}", "9").encode())) \
                if name == "antenna_status" else {}

        self.poller._fetch = fetch
        self.poller.start()
        self.assertTrue(started.wait(5))
        self.poller.retarget("127.0.0.2")
        release.set()
        self.assertTrue(wait_for(lambda: self.web("antenna_status")))
        self.assertFalse(any("old hub" in r["text"] for r in self.records("bridge")))
        self.assertTrue(all(h == "127.0.0.2" for h in hosts[1:]), hosts)

    def test_nan_readings_dropped(self):
        root = ws.check_reply("power", REPLIES["power"].replace("37.6", "nan").replace("18.1", "inf").encode())
        p = ws.parse_power(root)
        self.assertEqual((p["hub"]["temp_celsius"], p["antenna"]["lnb"]), (None, None))
        json.dumps(p, allow_nan=False)

    def test_retarget_drops_replies_for_the_old_hub(self):
        self.poller.start()
        self.assertTrue(wait_for(lambda: self.web("antenna_status")))
        other = FakeWebService()
        try:
            self.poller.port = other.port
            self.poller.retarget("127.0.0.1")
            n = len(self.fake.requests)
            self.assertTrue(wait_for(lambda: len(other.requests) >= 3))
            time.sleep(0.2)
            self.assertLessEqual(len(self.fake.requests), n + 1)  # at most the one already in flight
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()
