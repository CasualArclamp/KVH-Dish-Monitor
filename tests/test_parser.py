import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from tvhub_parser import (LineSplitter, fill_times, format_log_line, load_records,  # noqa: E402
                          parse_line, read_log)

# Real captures with the GPS position moved and the serial number blanked.
SAMPLE_LOG = os.path.join(ROOT, "samples", "pol-switch.log")        # V/L -> lock lost -> H/L -> re-acquire
INSTALL_LOG = os.path.join(ROOT, "samples", "install-restart.log")  # satellite install + ZAP restart

# (line, expected subset of the parsed record). Lines are from the project notes and tvhub.log.
CASES = [
    ("+POS:  17.7  55.8 -22.8 3406 11.83 166EN TRACKSAT",
     dict(kind="pos", az=17.7, el=55.8, skew=-22.8, rf=3406, snr=11.83, sat="166EN", substate="TRACKSAT", lock=True)),
    ("+POS:  17.5  55.7 -22.8  826  0.00 166EN TRACKSAT", dict(kind="pos", rf=826, snr=0.0, lock=False)),
    ("+BST:  17.5  54.8  -5.0", dict(kind="bst", az=17.5, el=54.8, third=-5.0)),
    ("+$GPRMC,062539.000,A,2725.0000,S,15315.0000,E,0.00,114.49,260926,,,A*79",
     dict(kind="gprmc", cs_ok=True, status="A", mode="A", lat=-27.4166667, lon=153.25, cog=114.49)),
    ("$GPRMC,063124.000,A,2725.0009,S,15315.0012,E,0.00,114.49,260926,,,D*7F", dict(kind="gprmc", cs_ok=True)),
    ("+VOLTAGE,0,ANT 42  VDC,39.5", dict(kind="voltage", idx=0, rail="ANT 42 VDC", value=39.5)),
    ("+VOLTAGE,2,ANT 8   VDC,8.1 ", dict(kind="voltage", idx=2, rail="ANT 8 VDC", value=8.1)),
    ("+VOLTAGE,4,ANT LNB VDC,18.1", dict(kind="voltage", idx=4, value=18.1, lnb_pol="H")),
    ("+VOLTAGE,4,ANT LNB VDC,13.2", dict(kind="voltage", lnb_pol="V")),
    ("+STATE: Tracking", dict(kind="state", state="Tracking", reply=False)),
    (">STATE: Tracking", dict(kind="state", state="Tracking", reply=True)),
    ("+Sleeping", dict(kind="state", state="Sleeping", bare=True)),
    ("+STATUS: 00000000 00000000", dict(kind="status", ok=True, words=["00000000", "00000000"])),
    ("+STATUS: 00000010 00000000", dict(kind="status", ok=False)),
    ("TEMP =  56.4 deg C", dict(kind="temp", temp=56.4)),
    ("Operational Hours = 17693.5", dict(kind="hours", hours=17693.5)),
    ("RF: AGC=60960", dict(kind="rf_agc", agc=60960, cat="rf")),
    ("RF: AGCON", dict(kind="rf_flag", flag="AGCON")),
    ("RF: SDOFF", dict(kind="rf_flag", flag="SDOFF")),
    ("RF: Power: 60383", dict(kind="rf_power", power=60383)),
    ("RF: Normalize ~ First HW Lock", dict(kind="rf_normalize", reason="First HW Lock", hw_lock=True, sd_lock=False)),
    ("RF: Normalize ~ First SD Lock", dict(kind="rf_normalize", sd_lock=True)),
    ("RF: Normalize ~ Out of range", dict(kind="rf_normalize", reason="Out of range")),
    ("RF: Y,Ignore ID,0x0001", dict(kind="rf_id", code="Y", text="Ignore ID", id="0x0001")),
    ("RF: I,1,60952,11.73,3550,1", dict(kind="rf_i", agc=60952, snr=11.73, rf=3550)),
    ("RF: FREQ,166EN,12606,30000,5/6,0XFFFE,QDVB,H,L,10700",
     dict(kind="rf_freq", sat="166EN", freq=12606, sr=30000, fec="5/6", nid="0XFFFE", decoder="QDVB",
          pol="H", band="L", lo=10700)),
    ("RF: SATINSTALL,USER6I", dict(kind="rf_satinstall", sat="USER6I")),
    ("RF: SATCONFIG,USER6I,0,11804,30000,1/2,0XFFFE,V,H,LQPSK",
     dict(kind="rf_satconfig", sat="USER6I", slot=0, freq=11804, sr=30000, fec="1/2", nid="0XFFFE",
          pol="V", band="H", decoder="LQPSK")),
    ("RF: LNB,13/18V,10700,N,ON,OFF,N,OFF", dict(kind="rf_lnb", switching="13/18V", lo=10700)),
    ("+RF: S,166EN,H,L,V", dict(kind="rf_select", sat="166EN", pol="H", band="L", async_=True)),
    ("RF: LOCKRESET", dict(kind="rf_flag", flag="LOCKRESET")),
    ("RF: something new", dict(kind="rf_other", text="something new")),
    ("+*** Entering Search Mode 0 ***", dict(kind="mode", search_mode=0, search_mode_name="local")),
    ("+*** Entering Search Mode 1 ***", dict(kind="mode", search_mode=1, search_mode_name="full sweep")),
    ("+*** Entering Tracking ***", dict(kind="mode", text="Entering Tracking")),
    ("*** Conscan Search ***", dict(kind="mode", text="Conscan Search")),
    ("+*** Tracking 166EN ***", dict(kind="mode", sat="166EN")),
    ("+*** Entering Track Sat Lost ***", dict(kind="mode", text="Entering Track Sat Lost")),
    ("Searching for 166EN, Threshold = 800", dict(kind="search_target", sat="166EN", threshold=800)),
    ("SetupSearchMoves: AZ = 180.00, EL =  54.68", dict(kind="search_setup", az=180.0, el=54.68)),
    ("bSearchMovesComplete: AZ =  17.20, EL =  56.79 SKEW = -22.80",
     dict(kind="search_moves_done", az=17.2, el=56.79, skew=-22.8)),
    ("Start Search: AZ =  17.20, EL =  56.78, SKEW = -22.80, RF =  500",
     dict(kind="search_start", az=17.2, el=56.78, skew=-22.8, rf=500)),
    ("At Start/End AZ = 177.70, EL = 57.52", dict(kind="search_bound", az=177.7, el=57.52)),
    ("Satellite Found: AZ =  14.6, EL =  56.0, RF = 2554", dict(kind="sat_found", az=14.6, el=56.0, rf=2554)),
    ("Conscan=>sleep: sat in 1 deg window.",
     dict(kind="transition", from_mode="Conscan", to_mode="sleep", reason="sat in 1 deg window.")),
    ("Sleep=>CONSCAN: RF below stop threshold.", dict(kind="transition", from_mode="Sleep", to_mode="CONSCAN")),
    ("Boresight AZ   17.66, EL   55.79", dict(kind="boresight", az=17.66, el=55.79)),
    ("Sleep SNR Wakeup Limits: Lo 10.29, Hi 13.29", dict(kind="sleep_limits", lo_db=10.29, hi_db=13.29)),
    ("Sleep: will now detect window exit.", dict(kind="note", text="will now detect window exit.")),
    ("AZDist -9.00, ELDist  1.13", dict(kind="azel_dist", az=-9.0, el=1.13)),
    ("Avg:  54.6  54.7  54.7  54.6  54.6", dict(kind="avg", vals=[54.6, 54.7, 54.7, 54.6, 54.6])),
    ("Saved Sat Pos: AZ =  17.66, EL =  54.63", dict(kind="saved_pos", az=17.66, el=54.63)),
    ("Gyro Bias Residue: AZ = -1.9078e-03 EL = -6.7994e-02", dict(kind="gyro_residue", az=-1.9078e-03, el=-6.7994e-02)),
    ("Old Gyro Bias: AZ = 1360.67 EL = 1355.38", dict(kind="gyro_bias", which="Old", az=1360.67, el=1355.38)),
    ("New Gyro Bias: AZ = 1360.67 EL = 1355.45", dict(kind="gyro_bias", which="New")),
    ("Saved Track Bias Az = 1360.35, El = 1354.99, Temp = 60 deg C",
     dict(kind="track_bias", az=1360.35, el=1354.99, temp=60.0)),
    ("Drift OR = 8.1746e-02 AzCorr 5.7889e-02 ElCorr -7.3572e-02",
     dict(kind="drift", drift=8.1746e-02, az_corr=5.7889e-02, el_corr=-7.3572e-02)),
    ("+AZOFFSET, 0.00,V", dict(kind="azoffset", offset=0.0, flag="V")),
    ("=>SAT,166EN,V,L", dict(kind="echo", cmd="SAT,166EN,V,L")),
    ("SAT,166EN,H,L", dict(kind="local_echo", cmd="SAT,166EN,H,L")),
    ("state", dict(kind="local_echo")),
    ("+SAT,USER6I,V,H", dict(kind="sat_sel", sat="USER6I", pol="V", band="H")),
    ("+SATINSTALL,USER6I", dict(kind="satinstall", sat="USER6I")),
    ("+GPS,2725.00,S,15315.00,E,A", dict(kind="gps_reply", fix="A", lat=-27.4166667, lon=153.25)),
    ("BOGUS Unknown command", dict(kind="unknown_cmd", cmd="BOGUS")),
    ("SLEEP=on", dict(kind="setting", name="SLEEP", value="on")),
    ("SIDELOBE = OFF", dict(kind="setting", name="SIDELOBE", value="OFF")),
    ("SEARCHTIMEOUT=ON 12 hour limit", dict(kind="setting", name="SEARCHTIMEOUT", value="ON 12 hour limit")),
    ("ANTLNB,19-0864 AUST DUA,L,13/18V,10700,N,ON,OFF,N,OFF",
     dict(kind="antlnb", model="19-0864 AUST DUA", lnb_type="L", switching="13/18V", lo=10700)),
    ("Hardware Version 01", dict(kind="hw_version", version="01")),
    ("+KVH TracVision TV6 Rev J - Version 2.50 - Serial Number 000000000 - SystemID TV6SK",
     dict(kind="version", model="TracVision TV6", rev="J", version="2.50", serial="000000000", system_id="TV6SK")),
    ("TV-HUB Maintenance port connection", dict(kind="banner")),
    ("ANT 42 VDC: OK; 41.8", dict(kind="power_test", rail="ANT 42 VDC", result="OK", value=41.8)),
    ("Limit Switch Status: PASS", dict(kind="limit_switch", result="PASS")),
    ("MTR TV6 Rev C v2.00 (04-0867)", dict(kind="part_version", name="MTR TV6", rev="C", version="2.00", part="04-0867")),
    # seen during a satellite install + restart (tvhub2.log)
    ("+SATSETUP,USER6I,156.00,-45.0", dict(kind="satsetup", sat="USER6I", lon=156.0, skew_offset=-45.0)),
    ("+SATCONFIG,166EN,0,12549,04279,2/3,0XFFFE,V,H,L8PSK",
     dict(kind="satconfig", sat="166EN", slot=0, freq=12549, sr=4279, fec="2/3", pol="V", band="H", decoder="L8PSK")),
    ("+SATCONFIG,USER6I,99,F1", dict(kind="satconfig", sat="USER6I", slot=99, checksum="F1")),
    ("+SATCK,USER2,D2", dict(kind="satck", sat="USER2", checksum="D2")),
    ("+SATINSTALL,USER6I,166EN,USER2,USER6", dict(kind="satinstall", sat="USER6I", sats=["USER6I", "166EN", "USER2", "USER6"])),
    ("RF: SATINSTALL,USER6I,166EN,USER2,USER6", dict(kind="rf_satinstall", sats=["USER6I", "166EN", "USER2", "USER6"])),
    ("+ZAP", dict(kind="restart")),
    ("LNB VOLTAGE: Expected 18.0, Actual 13.0", dict(kind="lnb_check", expected=18.0, actual=13.0, ok=False)),
    ("SAT: Polarization/Band <H/L> is invalid", dict(kind="sat_error", text="Polarization/Band <H/L> is invalid")),
    ("+GPS: USER6I AZ =   5.7, EL = 57.5, SKEW = 39.8", dict(kind="look_angles", sat="USER6I", az=5.7, el=57.5, skew=39.8)),
    ("+GPS: UTC: 070838.000, Lat: 2725.01S, Long: 15315.00E", dict(kind="gps_status", utc_time="070838.000")),
    ("+SystemID = 14 - TV6SK", dict(kind="system_id", sysid="14", name="TV6SK")),
    ("+RATE BIAS: PASS", dict(kind="selftest", name="RATE BIAS", result="PASS")),
    ("+RF COMM: PASS", dict(kind="selftest", name="RF COMM", result="PASS")),
    ("+Operational Hours = 17694.0", dict(kind="hours", hours=17694.0)),
    ("+SIDELOBE = OFF", dict(kind="setting", name="SIDELOBE", value="OFF")),
    ("+MTRVER,C,2.00,04-0867", dict(kind="part_version", name="MTR", rev="C", version="2.00", part="04-0867")),
    ("SKEW: SKEW TV6 REV C VER 2.00 SW 04-0868", dict(kind="part_version", name="SKEW", rev="C", part="04-0868")),
    ("+RF: SM TVRO RF REV G VER 2.50 SW 04-0858", dict(kind="part_version", name="RF", rev="G", version="2.50")),
    ("Saved Cold : Az 1363.95  El 1358.10    4 deg C", dict(kind="bias_cal", label="Saved Cold", az=1363.95, temp=4.0)),
    ("Current : X 1351.30  Y 1361.16  Z 1368.33   49 deg C", dict(kind="bias_xyz", x=1351.30, temp=49.0)),
    ("AzDiff:9.046875 = Computed:1361.046875 - FactCal:1352.000000", dict(kind="bias_diff", axis="Az", diff=9.046875)),
    ("Bias Fit Az =   -0.06, El =   -0.07 (Counts/degC)", dict(kind="bias_fit", az=-0.06, el=-0.07)),
    ("AccelMinMax Roll    0.6, Pitch    0.5, AzBiasDelta    0.1", dict(kind="accel", roll=0.6, pitch=0.5)),
    ("MTR 20  AZ 4652  EL 1194  SKS 20  SKW 0000", dict(kind="mtr_status", az=4652.0, skw="0000")),
    ("EE Page Write 0 76 58 USER6I", dict(kind="ee_write", page="0", sat="USER6I")),
    ("INSTALL_WAIT_CONFIG1", dict(kind="install_note")),
    ("uiSatsToInstall,4", dict(kind="install_note")),
    ("+Limit Switch Test", dict(kind="boot_note", text="Limit Switch Test")),
    ("Using Computed Bias - Stationary", dict(kind="boot_note")),
    ("Limit Switch Status: PASS", dict(kind="limit_switch", result="PASS")),
    (">STATE: Idle", dict(kind="state", state="Idle", reply=True)),
    ("!", dict(kind="unparsed")),
    ("Some brand new line", dict(kind="unparsed")),
]


class ParseLineTests(unittest.TestCase):
    def test_cases(self):
        for line, expected in CASES:
            with self.subTest(line=line):
                rec = parse_line(line)
                for key, want in expected.items():
                    key = "async" if key == "async_" else key
                    got = rec.get(key)
                    if isinstance(want, float):
                        self.assertAlmostEqual(got, want, places=6, msg=f"{key} in {rec}")
                    else:
                        self.assertEqual(got, want, f"{key} in {rec}")

    def test_bad_checksum_flagged(self):
        rec = parse_line("+$GPRMC,062539.000,A,2725.0000,S,15315.0000,E,0.00,114.49,260926,,,A*78")
        self.assertEqual(rec["kind"], "gprmc")
        self.assertFalse(rec["cs_ok"])

    def test_rmc_utc(self):
        rec = parse_line("+$GPRMC,062539.000,A,2725.0000,S,15315.0000,E,0.00,114.49,260926,,,A*79")
        want = datetime(2026, 9, 26, 6, 25, 39, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(rec["utc"], want, places=3)

    def test_every_record_has_kind_and_cat(self):
        for line, _ in CASES:
            rec = parse_line(line)
            self.assertIn("kind", rec)
            self.assertIn("cat", rec)


class LineSplitterTests(unittest.TestCase):
    def test_crlf_split_across_reads(self):
        sp = LineSplitter()
        self.assertEqual(sp.feed(b"+STATE: Tracking\r"), ["+STATE: Tracking"])
        self.assertEqual(sp.feed(b"\n+BST:  1.0  2.0  3.0\r\n"), ["+BST:  1.0  2.0  3.0"])
        self.assertFalse(sp.pending)

    def test_mixed_terminators_and_partial(self):
        sp = LineSplitter()
        self.assertEqual(sp.feed(b"a\nb\rc\r\nd"), ["a", "b", "c"])
        self.assertTrue(sp.pending)
        self.assertEqual(sp.feed(b"ef\n"), ["def"])

    def test_flush_and_control_chars(self):
        sp = LineSplitter()
        self.assertEqual(sp.feed(b"\x00!"), [])
        self.assertEqual(sp.flush(), ["!"])
        self.assertEqual(sp.flush(), [])

    def test_utf8_split_across_reads(self):
        sp = LineSplitter()
        data = "TEMP = 56.4 °C\n".encode("utf-8")
        cut = data.index(b"\xb0")  # middle of the two-byte degree sign
        self.assertEqual(sp.feed(data[:cut]), [])
        self.assertEqual(sp.feed(data[cut:]), ["TEMP = 56.4 °C"])


class TimingTests(unittest.TestCase):
    def test_fill_times_interpolates_and_extrapolates(self):
        out = fill_times([None, 10.0, None, None, 16.0, None])
        self.assertEqual(out, [8.0, 10.0, 12.0, 14.0, 16.0, 18.0])

    def test_fill_times_ignores_backwards_anchor(self):
        out = fill_times([0.0, None, 5.0, None, 4.0])
        self.assertEqual(out[:3], [0.0, 2.5, 5.0])
        self.assertTrue(all(b >= a for a, b in zip(out, out[1:])))

    def test_fill_times_without_anchors_ends_at_hint(self):
        self.assertEqual(fill_times([None, None, None], default_step=2.0, end_hint=100.0), [96.0, 98.0, 100.0])

    @unittest.skipUnless(os.path.exists(SAMPLE_LOG), "sample capture not present")
    def test_sample_log_uses_gps_time(self):
        entries, timing = read_log(SAMPLE_LOG)
        self.assertEqual(timing, "gps")
        times = [t for t, _, _ in entries]
        self.assertTrue(all(b >= a for a, b in zip(times, times[1:])))
        for t, _, text in entries:
            if "$GPRMC,063124.000" in text:
                want = datetime(2026, 9, 26, 6, 31, 24, tzinfo=timezone.utc).timestamp()
                self.assertAlmostEqual(t, want, places=3)
                break
        else:
            self.fail("anchor line not found")

    def test_bridge_log_round_trip(self):
        rows = [(1790000000.123, "rx", "+STATE: Tracking"), (1790000001.5, "tx", "STATE"),
                (1790000002.0, "bridge", "connected to 192.168.50.214:50001")]
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False, encoding="utf-8") as fh:
            for t, src, text in rows:
                fh.write(format_log_line(t, src, text) + "\n")
            path = fh.name
        try:
            entries, timing = read_log(path)
        finally:
            os.unlink(path)
        self.assertEqual(timing, "recorded")
        self.assertEqual([(src, text) for _, src, text in entries], [(s, x) for _, s, x in rows])
        for (t, _, _), (want, _, _) in zip(entries, rows):
            self.assertAlmostEqual(t, want, places=3)


@unittest.skipUnless(os.path.exists(SAMPLE_LOG), "sample capture not present")
class SampleLogTests(unittest.TestCase):
    def test_no_unparsed_lines(self):
        records, _ = load_records(SAMPLE_LOG)
        unparsed = [r["raw"] for r in records if r["kind"] == "unparsed"]
        self.assertEqual(unparsed, [])

    def test_all_nmea_checksums_valid(self):
        records, _ = load_records(SAMPLE_LOG)
        rmc = [r for r in records if r["kind"] == "gprmc"]
        self.assertGreater(len(rmc), 10)
        self.assertTrue(all(r["cs_ok"] for r in rmc))

    def test_pol_switch_sequence(self):
        records, _ = load_records(SAMPLE_LOG)
        kinds = [(r["kind"], r["raw"]) for r in records]
        i = kinds.index(("local_echo", "SAT,166EN,V,L"))
        self.assertEqual(kinds[i + 1], ("echo", "=>SAT,166EN,V,L"))
        self.assertEqual(kinds[i + 3][0], "sat_sel")
        self.assertIn(("rf_select", "+RF: S,166EN,V,L,V"), kinds[i:i + 8])


@unittest.skipUnless(os.path.exists(INSTALL_LOG), "sample capture not present")
class InstallLogTests(unittest.TestCase):
    def test_only_known_leftovers_unparsed(self):
        records, _ = load_records(INSTALL_LOG)
        leftovers = {r["raw"] for r in records if r["kind"] == "unparsed"}
        # "V42" (meaning unknown) and serial noise from the restart
        self.assertTrue(all(raw == "V42" or "�" in raw for raw in leftovers), leftovers)

    def test_rf_board_and_antenna_satconfig_agree(self):
        records, _ = load_records(INSTALL_LOG)
        fields = ("freq", "sr", "fec", "nid", "pol", "band", "decoder")
        rf = {(r["sat"], r["slot"]): tuple(r[f] for f in fields) for r in records if r["kind"] == "rf_satconfig"}
        ant = {(r["sat"], r["slot"]): tuple(r[f] for f in fields)
               for r in records if r["kind"] == "satconfig" and r["slot"] != 99}
        self.assertEqual(len(ant), 16)
        self.assertEqual(rf, ant)
        self.assertEqual(rf[("166EN", 2)], (12549, 4279, "2/3", "0XFFFE", "V", "L", "L8PSK"))


if __name__ == "__main__":
    unittest.main()
