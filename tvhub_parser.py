"""Parser for the KVH TracVision TV6 / TV-Hub maintenance stream (TCP port 50001).

Stdlib only. Turns each text line of the stream into a flat record dict:

    {"kind": "pos", "cat": "telemetry", "az": 17.7, "el": 55.8, "skew": -22.8,
     "rf": 3406, "snr": 11.83, "sat": "166EN", "substate": "TRACKSAT", "lock": True}

make_record() adds the common fields seq / t (epoch seconds) / src ("rx" = from the
TV-Hub, "tx" = a command we sent, "bridge" = a note from the bridge itself) / raw.
Lines that match no rule come back as kind "unparsed" so new line types stay visible.

Also here: LineSplitter (bytes -> lines for a live socket), read_log() (loads either a
plain `ncat -o` capture or a timestamped bridge log, synthesising timestamps from the
$GPRMC fixes when the file has none) and a small CLI:

    python tvhub_parser.py tvhub.log                  # summary + unparsed lines
    python tvhub_parser.py tvhub.log --events         # event timeline
    python tvhub_parser.py tvhub.log --csv ticks.csv  # +POS/+BST ticks as CSV
    python tvhub_parser.py tvhub.log --json           # every record, one JSON per line
"""

from __future__ import annotations

import argparse
import codecs
import csv
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Line parsing
# ---------------------------------------------------------------------------

_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_RE_NUM_GROUP = re.compile(r"\(\?P<(\w+)>" + re.escape(_NUM) + r"\)")
_INT_FIELDS = {"rf", "threshold", "agc", "power", "slot", "freq", "sr", "lo", "idx", "search_mode", "current", "saved",
               "signal", "antenna"}


def _n(name: str) -> str:
    """Named regex group matching a number; its value is converted on match."""
    return rf"(?P<{name}>{_NUM})"


def _convert(groups: dict, numeric: frozenset) -> dict:
    out = {}
    for key, val in groups.items():
        if val is None:
            continue
        if key in numeric:
            num = float(val)
            out[key] = int(num) if key in _INT_FIELDS else num
        else:
            out[key] = val.strip()
    return out


# (kind, category, regex) tried in order against the stripped line, after the
# prefix-dispatched parsers below have had their turn.
_SIMPLE_RULES = [
    ("temp", "telemetry", rf"^TEMP\s*=\s*{_n('temp')}\s*deg\s*C\b"),
    ("hours", "telemetry", rf"^\+?Operational Hours\s*=\s*{_n('hours')}"),
    ("lnb_check", "event", rf"^LNB VOLTAGE:\s*Expected\s+{_n('expected')},\s*Actual\s+{_n('actual')}"),
    # sidelobe check (SIDELOBE=ON): AGC compared with the saved main-beam AGC minus a margin
    ("sidelobe_check", "event",
     rf"^Current\s+{_n('current')},\s*Saved\s+{_n('saved')},\s*Threshold\s+{_n('threshold')},\s*SNR\s+{_n('snr')}"),
    ("sidelobe_agc", "housekeeping",
     rf"^AGC Saved\s+{_n('saved')},\s*Threshold\s+{_n('threshold')},\s*SNR\s+{_n('snr')}"),
    ("beam_found", "event", rf"^New Beam Found:\s*AZ\s*=\s*{_n('az')},\s*EL\s*=\s*{_n('el')},\s*RF\s*=\s*{_n('rf')}"),
    ("beam_move", "event",
     rf"^Move to New Beam\s+(?P<axis>AZ|EL)\s+Cur\s+{_n('cur')},\s*Beam\s*=\s*{_n('beam')},\s*Unwrap\s*=\s*{_n('unwrap')}"),
    ("main_beam", "event", r"^Main Beam\s*$"),
    ("sat_change", "event", r"^\+?Satellite Change:\s*(?P<from_sat>\S+)\s+to\s+(?P<to_sat>\S+)"),
    ("search_bound", "event", rf"^At End Position\s+AZ\s*=\s*{_n('az')},\s*EL\s*=\s*{_n('el')}"),
    ("search_step", "event", rf"^Target Position\s+{_n('target')}\s*$"),  # rows of the search raster (EL?)
    ("note", "event", r"^(?P<text>EL Min/Max.*)$"),
    ("look_angles", "boot",
     rf"^\+GPS:\s*(?P<sat>[^:\s]+)\s+AZ\s*=\s*{_n('az')},\s*EL\s*=\s*{_n('el')},\s*SKEW\s*=\s*{_n('skew')}"),
    ("sat_error", "reply", r"^SAT:\s*(?P<text>.+)$"),
    ("system_id", "boot", r"^\+?SystemID\s*=\s*(?P<sysid>\d+)\s*-\s*(?P<name>\S+)"),
    ("selftest", "boot", r"^\+?(?P<name>[A-Z][A-Z ]*[A-Z]):\s*(?P<result>PASS|FAIL\w*)\s*$"),
    ("bias_cal", "housekeeping",
     rf"^(?P<label>[A-Za-z][A-Za-z ]*?)\s*:\s*Az\s+{_n('az')}\s+El\s+{_n('el')}\s+{_n('temp')}\s*deg\s*C"),
    ("bias_xyz", "housekeeping",
     rf"^Current\s*:\s*X\s+{_n('x')}\s+Y\s+{_n('y')}\s+Z\s+{_n('z')}\s+{_n('temp')}\s*deg\s*C"),
    ("bias_diff", "housekeeping",
     rf"^(?P<axis>Az|El)Diff:\s*{_n('diff')}\s*=\s*Computed:\s*{_n('computed')}\s*-\s*FactCal:\s*{_n('factcal')}"),
    ("bias_fit", "housekeeping", rf"^Bias Fit Az\s*=\s*{_n('az')},\s*El\s*=\s*{_n('el')}"),
    ("accel", "housekeeping",
     rf"^AccelMinMax Roll\s+{_n('roll')},\s*Pitch\s+{_n('pitch')},\s*AzBiasDelta\s+{_n('az_bias_delta')}"),
    ("mtr_status", "boot",  # meaning of the fields unknown
     rf"^MTR\s+{_n('mtr')}\s+AZ\s+{_n('az')}\s+EL\s+{_n('el')}\s+SKS\s+{_n('sks')}\s+SKW\s+(?P<skw>\w+)"),
    ("ee_write", "boot", r"^EE Page Write\s+(?P<page>\d+)\s+(?P<a>\d+)\s+(?P<b>\d+)(?:\s+(?P<sat>\S+))?\s*$"),
    ("ee_locked", "reply", r"^EE Locked - Unable to write\s*$"),
    # After the EE page writes of a satellite install; meaning of the two numbers unknown.
    ("rf_antenna", "housekeeping", rf"^RF\s+{_n('rf')}\s+Antenna\s+{_n('antenna')}\s*$"),
    ("install_note", "reply", r"^(?P<text>INSTALL_\w+|uiSatsToInstall,\d+)\s*$"),
    ("boot_note", "boot", r"^\+?(?P<text>(?:\w+ )?Limit Switch Test.*|Using Computed Bias.*|Waiting For Bias Trim)$"),
    ("part_version", "boot",
     r"^\+(?P<name>[A-Z]+)VER,(?P<rev>[A-Z0-9]+),(?P<version>[\d.]+),(?P<part>[\w-]+)\s*$"),
    ("part_version", "boot",
     r"^(?P<name>[A-Z]+):\s*(?P<desc>.+?)\s+REV\s+(?P<rev>\w+)\s+VER\s+(?P<version>[\d.]+)\s+SW\s+(?P<part>[\w-]+)"),
    ("part_version", "boot",  # e.g. "TV-HUB DISEQC REV B VER 1.01 SW 04-0874"
     r"^(?P<name>[A-Z][A-Z0-9 -]*?)\s+REV\s+(?P<rev>\w+)\s+VER\s+(?P<version>[\d.]+)\s+SW\s+(?P<part>[\w-]+)\s*$"),
    ("azoffset", "telemetry", rf"^\+?AZOFFSET,\s*{_n('offset')}\s*(?:,\s*(?P<flag>\w*))?"),
    ("search_target", "event", rf"^Searching for (?P<sat>[^,\s]+),?\s+Threshold\s*=\s*{_n('threshold')}"),
    ("search_setup", "event", rf"^SetupSearchMoves:\s*AZ\s*=\s*{_n('az')},\s*EL\s*=\s*{_n('el')}"),
    ("search_moves_done", "event",
     rf"^bSearchMovesComplete:\s*AZ\s*=\s*{_n('az')},\s*EL\s*=\s*{_n('el')}\s*,?\s*SKEW\s*=\s*{_n('skew')}"),
    ("search_start", "event",
     rf"^Start Search:\s*AZ\s*=\s*{_n('az')},\s*EL\s*=\s*{_n('el')},\s*SKEW\s*=\s*{_n('skew')},\s*RF\s*=\s*{_n('rf')}"),
    ("search_bound", "event", rf"^At Start/End\s+AZ\s*=\s*{_n('az')},\s*EL\s*=\s*{_n('el')}"),
    ("sat_found", "event", rf"^Satellite Found:\s*AZ\s*=\s*{_n('az')},\s*EL\s*=\s*{_n('el')},\s*RF\s*=\s*{_n('rf')}"),
    ("boresight", "event", rf"^Boresight\s+AZ\s+{_n('az')},\s*EL\s+{_n('el')}"),
    ("sleep_limits", "event", rf"^Sleep SNR Wakeup Limits:\s*Lo\s+{_n('lo_db')},\s*Hi\s+{_n('hi_db')}"),
    ("azel_dist", "event", rf"^AZDist\s+{_n('az')},\s*ELDist\s+{_n('el')}"),
    ("saved_pos", "housekeeping", rf"^Saved Sat Pos:\s*AZ\s*=\s*{_n('az')},\s*EL\s*=\s*{_n('el')}"),
    ("gyro_residue", "housekeeping", rf"^Gyro Bias Residue:\s*AZ\s*=\s*{_n('az')}\s*,?\s*EL\s*=\s*{_n('el')}"),
    ("gyro_bias", "housekeeping", rf"^(?P<which>Old|New) Gyro Bias:\s*AZ\s*=\s*{_n('az')}\s*,?\s*EL\s*=\s*{_n('el')}"),
    ("track_bias", "housekeeping",
     rf"^Saved Track Bias\s+Az\s*=\s*{_n('az')},\s*El\s*=\s*{_n('el')},\s*Temp\s*=\s*{_n('temp')}"),
    ("drift", "housekeeping", rf"^Drift OR\s*=\s*{_n('drift')}\s+AzCorr\s+{_n('az_corr')}\s+ElCorr\s+{_n('el_corr')}"),
    ("limit_switch", "boot", r"^Limit Switch Status:\s*(?P<result>\S+)"),
    ("power_test", "boot", rf"^(?P<rail>ANT\s.*?VDC):\s*(?P<result>\w+)\s*;\s*{_n('value')}"),
    ("ee_status", "boot", r"^EE STATUS\b[\s:]*(?P<text>.*)$"),
    ("hw_version", "reply", r"^Hardware Version\s+(?P<version>\S+)"),
    ("unknown_cmd", "reply", r"^(?P<cmd>.+?)\s+Unknown command\s*$"),
    ("banner", "boot", r"^TV-HUB Maintenance port connection"),
    ("transition", "event", r"^(?P<from_mode>\w+)=>(?P<to_mode>\w+):\s*(?P<reason>.*)$"),
    ("note", "event", r"^Sleep:\s*(?P<text>.*)$"),
    ("part_version", "boot",
     r"^(?P<name>.+?)\s+Rev\s+(?P<rev>[A-Z0-9]+)\s+v(?P<version>[\d.]+)\s*\((?P<part>[\w-]+)\)\s*,?\s*(?P<rest>.*)$"),
    # HELP output (Idle mode only), e.g. "AZ,XXXX     = Command a manual azimuth angle (0-3599)".
    # Must come before "setting": the description always starts with a capitalised word.
    ("help_entry", "reply", r"^(?P<cmd>[A-Z0-9@=][A-Z0-9,@=]*)\s+=\s+(?P<desc>[A-Z][a-z]+\b.*)$"),
    ("needs_mode", "reply", r"^(?P<cmd>\S+) requires (?P<mode>\w+) mode\.?\s*$"),
    # Replies to SIGLEVEL and TGTLOCATION (one line per installed satellite, EL and AZ in
    # tenths of a degree), and the antenna's answer to a garbled command.
    ("siglevel", "reply", rf"^Signal Strength\s*=\s*{_n('signal')}\s*$"),
    ("tgt_location", "reply", r"^Target Location:\s*(?P<sat>\S+)\s*=\s*E(?P<el>\d+),\s*A(?P<az>\d+)\s*$"),
    ("malformed", "reply", r"^(?P<cmd>.+?)\s+Malformed message\s*$"),
    # The antenna's acceptance of a manual AZ,XXXX / EL,XXX command (it echoes the padded value).
    ("manual_ack", "reply", r"^(?P<axis>AZ|EL),(?P<tenths>\d{3,4})$"),
    # ...and of a 0.1-degree step, as the bare digit (8/2 = EL up/down, 6/4 = AZ clockwise/counter-clockwise).
    ("jog_ack", "reply", r"^(?P<step>[2468])$"),
    ("setting", "reply", r"^\+?(?P<name>[A-Z][A-Z0-9_]*)\s*=\s*(?P<value>.+)$"),
]
_SIMPLE_RULES = [(kind, cat, re.compile(rx), frozenset(_RE_NUM_GROUP.findall(rx)))
                 for kind, cat, rx in _SIMPLE_RULES]

_RE_STATE = re.compile(r"^([+>])STATE:\s*(.*?)\s*$")
_RE_MODE = re.compile(r"^\+?\*{3}\s*(.*?)\s*\*{3}\s*$")
_RE_SEARCH_MODE = re.compile(r"^Entering Search Mode\s+(\d+)")
_RE_TRACKING_SAT = re.compile(r"^Tracking\s+(\S+)$")
_RE_VERSION = re.compile(
    r"^\+?KVH\s+(?P<model>.+?)\s+Rev\s+(?P<rev>\S+)\s+-\s+Version\s+(?P<version>\S+)"
    r"\s+-\s+Serial Number\s+(?P<serial>\S+)\s+-\s+SystemID\s+(?P<system_id>\S+)")
_RE_RF_ID = re.compile(r"^([A-Z]),([^,]*),(0[xX][0-9A-Fa-f]+)$")
_RE_ACK = re.compile(r"^\+([A-Z][A-Z0-9]{2,15})$")
_RE_RF_VERSION = re.compile(r"^(?P<desc>.+?)\s+REV\s+(?P<rev>\w+)\s+VER\s+(?P<version>[\d.]+)\s+SW\s+(?P<part>[\w-]+)")
_RE_GPS_STATUS = re.compile(
    r"^\+GPS:\s*UTC:\s*(?P<utc_time>[\d.]+),\s*Lat:\s*(?P<lat>[\d.]+)(?P<ns>[NS]),\s*Long:\s*(?P<lon>[\d.]+)(?P<ew>[EW])")
_RF_FLAGS = {"AGCON", "AGCOFF", "SDON", "SDOFF", "LOCKRESET", "CHECKID", "NORMON", "NORMOFF"}

# Command words we know of; a bare line that starts with one of these is the capturing
# terminal's own copy of a typed command (ncat -o logs both directions), not device output.
KNOWN_COMMAND_WORDS = {
    "STATE", "SAT", "SATINSTALL", "SATCK", "GPS", "=SERNUM", "@VER", "VERSION", "HOURS",
    "SIDELOBE", "SLEEP", "ANTLNB", "SEARCHTIMEOUT", "@FPGAVER", "STATUS", "HW", "ZAP", "HALT",
    "CLEAREE", "@CLEAREE", "@SAVE", "HELP", "THRESHOLD", "THRESH", "@THRESHOLD",
    "TRACK", "SMACK", "AZ", "EL", "TGTLOCATION", "SIGLEVEL", "DEBUGON", "DEBUGOFF", "SKEW",
}
_RE_LOCAL_CMD = re.compile(r"^([=@]?[A-Za-z][A-Za-z0-9]*)((?:,[^,\s]*)*)$")

SEARCH_MODE_NAMES = {0: "local", 1: "full sweep", 2: "REACQ"}
_JOG_STEPS = {"8": ("EL", 0.1), "2": ("EL", -0.1), "6": ("AZ", 0.1), "4": ("AZ", -0.1)}


def _num(tok: str) -> float:
    return float(tok)


def _nmea_coord(value: str, hemi: str) -> float | None:
    """ddmm.mmmm / dddmm.mmmm + hemisphere -> signed decimal degrees."""
    try:
        v = float(value)
    except ValueError:
        return None
    deg = int(v // 100)
    dec = deg + (v - deg * 100) / 60.0
    return round(-dec if hemi in ("S", "W") else dec, 7)


def _parse_nmea(s: str) -> dict:
    """s starts with '$'. Checks the checksum (cs_ok is None when the sentence has none);
    decodes RMC, keeps others generic."""
    star = s.rfind("*")
    body = s[1:star] if star >= 0 else s[1:]
    calc = 0
    for ch in body:
        calc ^= ord(ch)
    given = s[star + 1:star + 3] if star >= 0 else ""
    try:
        cs_ok = None if star < 0 else (len(given) == 2 and int(given, 16) == calc)
    except ValueError:
        cs_ok = False
    f = body.split(",")
    sentence = f[0]
    if sentence.endswith("RMC") and len(f) >= 10:
        rec = {
            "kind": "gprmc", "cat": "telemetry", "sentence": sentence, "cs_ok": cs_ok,
            "utc_time": f[1], "status": f[2],
            "lat": _nmea_coord(f[3], f[4]) if f[3] else None,
            "lon": _nmea_coord(f[5], f[6]) if f[5] else None,
            "date": f[9], "mode": f[12] if len(f) > 12 else "",
        }
        for key, idx in (("sog_kn", 7), ("cog", 8)):
            try:
                rec[key] = float(f[idx])
            except ValueError:
                rec[key] = None
        rec["utc"] = _rmc_epoch(f[9], f[1])
        return rec
    return {"kind": "nmea", "cat": "telemetry", "sentence": sentence, "cs_ok": cs_ok, "fields": f[1:]}


def _rmc_epoch(date: str, hms: str) -> float | None:
    """ddmmyy + hhmmss(.sss) -> POSIX seconds (UTC), or None."""
    if len(date) != 6 or len(hms) < 6:
        return None
    try:
        day, month, yy = int(date[0:2]), int(date[2:4]), int(date[4:6])
        hh, mm = int(hms[0:2]), int(hms[2:4])
        sec = float(hms[4:])
        base = datetime(2000 + yy if yy < 80 else 1900 + yy, month, day, hh, mm, tzinfo=timezone.utc)
    except ValueError:
        return None
    return base.timestamp() + sec


def _satconfig(p: list[str]) -> dict | None:
    """sat,slot,freq,sr,fec,nid,pol,band,decoder - or sat,99,<checksum> for the checksum slot."""
    try:
        if len(p) >= 9:
            return {"sat": p[0], "slot": int(p[1]), "freq": int(p[2]), "sr": int(p[3]), "fec": p[4],
                    "nid": p[5], "pol": p[6], "band": p[7], "decoder": p[8]}
        if len(p) == 3:
            return {"sat": p[0], "slot": int(p[1]), "checksum": p[2]}
    except ValueError:
        pass
    return None


def _parse_rf(body: str, async_: bool) -> dict:
    """Body of an 'RF:' / '+RF:' line (prefix removed)."""
    rec: dict = {"cat": "rf", "async": async_}
    if body.startswith("AGC="):
        try:
            return {**rec, "kind": "rf_agc", "agc": int(body[4:].strip())}
        except ValueError:
            pass
    if body in _RF_FLAGS:
        return {**rec, "kind": "rf_flag", "flag": body}
    if body.startswith("Power:"):
        try:
            return {**rec, "kind": "rf_power", "power": int(body[6:].strip())}
        except ValueError:
            pass
    if body.startswith("Normalize"):
        reason = body.split("~", 1)[1].strip() if "~" in body else body[9:].strip()
        return {**rec, "kind": "rf_normalize", "reason": reason,
                "hw_lock": "HW Lock" in reason, "sd_lock": "SD Lock" in reason}
    parts = [p.strip() for p in body.split(",")]
    head = parts[0]
    if head == "FREQ" and len(parts) >= 10:
        try:
            return {**rec, "kind": "rf_freq", "sat": parts[1], "freq": int(parts[2]), "sr": int(parts[3]),
                    "fec": parts[4], "nid": parts[5], "decoder": parts[6], "pol": parts[7],
                    "band": parts[8], "lo": int(parts[9])}
        except ValueError:
            pass
    if head == "SATCONFIG":
        fields = _satconfig(parts[1:])
        if fields:
            return {**rec, "kind": "rf_satconfig", **fields}
    m = _RE_RF_VERSION.match(body)
    if m:
        return {"kind": "part_version", "cat": "boot", "name": "RF", **m.groupdict()}
    m = re.match(r"^Sats Installed:\s*(\d+)$", body)
    if m:
        return {**rec, "kind": "rf_installed", "count": int(m.group(1))}
    if head == "SATINSTALL" and len(parts) >= 2:
        sats = [p for p in parts[1:] if p]
        return {**rec, "kind": "rf_satinstall", "sat": sats[0] if sats else "", "sats": sats}
    if head == "LNB" and len(parts) >= 3:
        out = {**rec, "kind": "rf_lnb", "switching": parts[1], "flags": parts[3:]}
        try:
            out["lo"] = int(parts[2])
        except ValueError:
            out["lo_text"] = parts[2]
        return out
    if head == "S" and len(parts) >= 4:
        # Last field (inferred): V = usable slot, I = invalid; I is repeated every few seconds for slots
        # whose SATCONFIG frequency is 0.
        flag = parts[4] if len(parts) > 4 else ""
        return {**rec, "kind": "rf_select", "sat": parts[1], "pol": parts[2], "band": parts[3],
                "valid": {"V": True, "I": False}.get(flag), "extra": parts[4:]}
    if head == "I" and len(parts) >= 2:
        try:
            vals = [float(p) for p in parts[1:]]
        except ValueError:
            vals = None
        if vals is not None:
            out = {**rec, "kind": "rf_i", "vals": vals}
            if len(vals) == 5:  # (inferred) I,?,AGC,SNR,RF,?
                out.update(agc=int(vals[1]), snr=vals[2], rf=int(vals[3]))
            return out
    m = _RE_RF_ID.match(body)
    if m:
        return {**rec, "kind": "rf_id", "code": m.group(1), "text": m.group(2).strip(), "id": m.group(3)}
    return {**rec, "kind": "rf_other", "text": body}


def _parse_pos(body: str) -> dict | None:
    tok = body.split()
    if len(tok) < 5:
        return None
    try:
        az, el, skew, rf, snr = _num(tok[0]), _num(tok[1]), _num(tok[2]), int(_num(tok[3])), _num(tok[4])
    except ValueError:
        return None
    rec = {"kind": "pos", "cat": "telemetry", "az": az, "el": el, "skew": skew, "rf": rf, "snr": snr,
           "sat": tok[5] if len(tok) > 5 else None, "substate": tok[6] if len(tok) > 6 else None,
           "lock": snr > 0}
    if len(tok) > 7:
        rec["extra"] = tok[7:]
    return rec


def _parse_bst(body: str) -> dict | None:
    try:
        vals = [_num(t) for t in body.split()]
    except ValueError:
        return None
    if len(vals) < 2:
        return None
    # Boresight: the TV-Hub's status page names these BORESIGHT AZIMUTH / ELEVATION / TILT.
    rec = {"kind": "bst", "cat": "telemetry", "az": vals[0], "el": vals[1]}
    if len(vals) > 2:
        rec["tilt"] = vals[2]
    if len(vals) > 3:
        rec["extra"] = vals[3:]
    return rec


def _parse_voltage(s: str) -> dict | None:
    parts = s.lstrip("+").split(",")
    if len(parts) < 4:
        return None
    try:
        idx, value = int(parts[1]), float(parts[-1])
    except ValueError:
        return None
    rail = " ".join(",".join(parts[2:-1]).split())
    rec = {"kind": "voltage", "cat": "telemetry", "idx": idx, "rail": rail, "value": value}
    if "LNB" in rail:
        # 13 V selects vertical, 18 V horizontal on this linear LNB.
        rec["lnb_pol"] = "off" if value < 10 else ("V" if value < 15.5 else "H")
    return rec


def parse_line(line: str) -> dict:
    """Classify one stream line. Always returns a dict with at least kind and cat."""
    s = line.strip()
    if not s:
        return {"kind": "blank", "cat": "unparsed"}

    if s.startswith("+POS:"):
        rec = _parse_pos(s[5:])
        if rec:
            return rec
    elif s.startswith("+BST:"):
        rec = _parse_bst(s[5:])
        if rec:
            return rec
    elif s[:1] == "$" or s[:2] in ("+$", "~$"):  # "~$": the TV-Hub's own copy, no checksum
        return _parse_nmea(s.lstrip("+~"))
    elif s.startswith("+VOLTAGE,"):
        rec = _parse_voltage(s)
        if rec:
            return rec
    elif s.startswith("RF:") or s.startswith("+RF:"):
        async_ = s.startswith("+")
        return _parse_rf(s[4 if async_ else 3:].strip(), async_)
    elif s.startswith("=>"):
        return {"kind": "echo", "cat": "command", "cmd": s[2:].strip()}

    m = _RE_STATE.match(s)
    if m:
        return {"kind": "state", "cat": "telemetry", "state": m.group(2), "reply": m.group(1) == ">"}
    if s == "+Sleeping":
        return {"kind": "state", "cat": "telemetry", "state": "Sleeping", "reply": False, "bare": True}
    if s.startswith("+STATUS:"):
        words = s[8:].split()
        try:
            ok = all(int(w, 16) == 0 for w in words)
        except ValueError:
            ok = None
        return {"kind": "status", "cat": "telemetry", "words": words, "ok": ok}

    m = _RE_MODE.match(s)
    if m:
        text = m.group(1)
        rec = {"kind": "mode", "cat": "event", "text": text}
        sm = _RE_SEARCH_MODE.match(text)
        if sm:
            rec["search_mode"] = int(sm.group(1))
            rec["search_mode_name"] = SEARCH_MODE_NAMES.get(rec["search_mode"], "?")
        tm = _RE_TRACKING_SAT.match(text)
        if tm:
            rec["sat"] = tm.group(1)
        return rec

    if s.startswith(">"):
        return {"kind": "reply", "cat": "reply", "text": s[1:].strip()}
    if s.startswith("+SAT,"):
        p = [x.strip() for x in s[5:].split(",")]
        rec = {"kind": "sat_sel", "cat": "reply", "sat": p[0]}
        if len(p) >= 3:
            rec.update(pol=p[1], band=p[2])
        if len(p) > 3:
            rec["extra"] = p[3:]
        return rec
    if s.startswith("+SATINSTALL,"):
        sats = [x.strip() for x in s[12:].split(",") if x.strip()]
        return {"kind": "satinstall", "cat": "reply", "sat": sats[0] if sats else "", "sats": sats}
    if s.startswith("+SATCONFIG,"):  # the antenna's copy, as sent at install; compare with RF: SATCONFIG
        fields = _satconfig([x.strip() for x in s[11:].split(",")])
        if fields:
            return {"kind": "satconfig", "cat": "reply", **fields}
    if s.startswith("+SATSETUP,"):
        p = [x.strip() for x in s[10:].split(",")]
        try:
            lon = float(p[1])
            rec = {"kind": "satsetup", "cat": "reply", "sat": p[0], "lon": lon - 360 if lon > 180 else lon,
                   "skew_offset": float(p[2]) if len(p) > 2 else None}
            if len(p) > 3:
                rec["extra"] = p[3:]
            return rec
        except (IndexError, ValueError):
            pass
    if s.startswith("+SATCK,"):
        p = [x.strip() for x in s[7:].split(",")]
        if len(p) >= 2:
            return {"kind": "satck", "cat": "reply", "sat": p[0], "checksum": p[1]}
    if s == "+ZAP":
        return {"kind": "restart", "cat": "event"}
    m = _RE_ACK.match(s)
    if m:  # the antenna acknowledging a bare command word: +HALT, +TRACK, +DEBUGON, ...
        return {"kind": "ack", "cat": "reply", "cmd": m.group(1)}
    m = _RE_GPS_STATUS.match(s)
    if m:
        return {"kind": "gps_status", "cat": "boot", "utc_time": m.group("utc_time"),
                "lat": _nmea_coord(m.group("lat"), m.group("ns")), "lon": _nmea_coord(m.group("lon"), m.group("ew"))}
    if s.startswith("+GPS,"):
        p = s[5:].split(",")
        if len(p) >= 4:
            return {"kind": "gps_reply", "cat": "reply", "lat": _nmea_coord(p[0], p[1]),
                    "lon": _nmea_coord(p[2], p[3]), "fix": p[4] if len(p) > 4 else ""}
    if s.startswith("+KVH") or s.startswith("KVH "):
        m = _RE_VERSION.match(s)
        if m:
            return {"kind": "version", "cat": "reply", **m.groupdict()}
        return {"kind": "version", "cat": "reply", "text": s.lstrip("+")}
    if s.startswith("Avg:"):
        try:
            return {"kind": "avg", "cat": "housekeeping", "vals": [float(v) for v in s[4:].split()]}
        except ValueError:
            pass
    if s.startswith("ANTLNB,"):
        p = [x.strip() for x in s[7:].split(",")]
        rec = {"kind": "antlnb", "cat": "reply", "model": p[0], "fields": p[1:]}
        if len(p) >= 4:
            rec.update(lnb_type=p[1], switching=p[2])
            try:
                rec["lo"] = int(p[3])
            except ValueError:
                pass
        return rec

    for kind, cat, rx, numeric in _SIMPLE_RULES:
        m = rx.match(s)
        if m:
            rec = {"kind": kind, "cat": cat, **_convert(m.groupdict(), numeric)}
            if kind == "lnb_check":
                rec["ok"] = abs(rec["expected"] - rec["actual"]) < 1.5
            elif kind == "tgt_location":
                rec["el"], rec["az"] = int(rec["el"]) / 10, int(rec["az"]) / 10
            elif kind == "manual_ack":
                rec["value"] = int(rec.pop("tenths")) / 10
            elif kind == "jog_ack":
                rec["axis"], rec["delta"] = _JOG_STEPS[rec["step"]]
            return rec

    m = _RE_LOCAL_CMD.match(s)
    if m:
        word = m.group(1).upper()
        if word in KNOWN_COMMAND_WORDS or word.startswith(("=", "@")):
            return {"kind": "local_echo", "cat": "command", "cmd": s}

    return {"kind": "unparsed", "cat": "unparsed"}


def make_record(text: str, t: float, src: str = "rx", seq: int | None = None) -> dict:
    """Build a full record: parsed fields plus seq/t/src/raw."""
    if src == "rx":
        rec = parse_line(text)
    elif src == "tx":
        rec = {"kind": "tx", "cat": "command", "cmd": text}
    elif src == "hub":  # a "KEY: value" line from the TV-Hub's status snapshot
        key, sep, value = text.partition(": ")
        rec = {"kind": "hub_info", "cat": "hub", "key": key.strip() if sep else None,
               "value": value.strip() if sep else text.strip()}
    elif src == "web":  # a reply from the TV-Hub's web service, as {"msg": name, "data": {...}} JSON
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
        if isinstance(obj, dict) and isinstance(obj.get("msg"), str):
            rec = {"kind": "hubweb", "cat": "hub", "msg": obj["msg"], "data": obj.get("data")}
        else:
            rec = {"kind": "unparsed", "cat": "unparsed"}
    else:
        rec = {"kind": "bridge", "cat": "bridge", "text": text}
    rec["t"] = round(t, 3)
    rec["src"] = src
    rec["raw"] = text
    if seq is not None:
        rec["seq"] = seq
    return rec


# ---------------------------------------------------------------------------
# Byte stream -> lines
# ---------------------------------------------------------------------------

_RE_EOL = re.compile(r"\r\n|\r|\n")
_RE_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class LineSplitter:
    """Incremental bytes -> text lines. Accepts CRLF, LF or bare CR as terminators,
    including a CRLF split across two reads. Control characters are dropped."""

    def __init__(self) -> None:
        self._dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buf = ""
        self._skip_lf = False

    @property
    def pending(self) -> bool:
        return bool(self._buf)

    def feed(self, data: bytes) -> list[str]:
        text = self._dec.decode(data)
        if self._skip_lf and text:
            if text.startswith("\n"):
                text = text[1:]
            self._skip_lf = False
        buf = self._buf + text
        parts = _RE_EOL.split(buf)
        self._buf = parts.pop()
        if not self._buf and buf.endswith("\r"):
            self._skip_lf = True
        return [line for line in (self._clean(p) for p in parts) if line]

    def flush(self) -> list[str]:
        """Return the unterminated remainder as a line (e.g. a prompt with no newline)."""
        line, self._buf = self._clean(self._buf), ""
        return [line] if line else []

    @staticmethod
    def _clean(s: str) -> str:
        return _RE_CTRL.sub("", s).rstrip()


# ---------------------------------------------------------------------------
# Log files
# ---------------------------------------------------------------------------

# Bridge log format: ISO-8601 local time with offset, TAB, RX|TX|--|WB, TAB, line
# (WB lines are the hub web service's replies as JSON; HB is only produced when reading an export).
_RE_TS_LINE = re.compile(
    r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:?\d\d)?)\t(RX|TX|--|HB|WB)\t(.*)$")
_TAG_TO_SRC = {"RX": "rx", "TX": "tx", "--": "bridge", "HB": "hub", "WB": "web"}  # WB: hub web service reply
_SRC_TO_TAG = {v: k for k, v in _TAG_TO_SRC.items()}
POS_INTERVAL_S = 3.5  # typical +POS cadence, used when a log has no GPS time at all


def format_log_line(t: float, src: str, text: str) -> str:
    stamp = datetime.fromtimestamp(t).astimezone().isoformat(timespec="milliseconds")
    return f"{stamp}\t{_SRC_TO_TAG[src]}\t{text}"


def _parse_iso(stamp: str) -> float | None:
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive -> local time
    return dt.timestamp()


def fill_times(times: list, default_step: float = 1.0, end_hint: float | None = None) -> list[float]:
    """Fill None entries by linear interpolation between known times (by line index);
    extrapolate beyond the ends at the average known rate. Known times that go
    backwards are ignored. With no known times at all, the last line gets end_hint."""
    n = len(times)
    known: list[tuple[int, float]] = []
    for i, t in enumerate(times):
        if t is not None and (not known or t >= known[-1][1]):
            known.append((i, t))
    if not known:
        end = end_hint if end_hint is not None else datetime.now().timestamp()
        return [end - (n - 1 - i) * default_step for i in range(n)]
    step = default_step
    if len(known) >= 2:
        (i0, t0), (i1, t1) = known[0], known[-1]
        if i1 > i0 and t1 > t0:
            step = (t1 - t0) / (i1 - i0)
    out: list[float] = [0.0] * n
    first_i, first_t = known[0]
    for i in range(first_i + 1):
        out[i] = first_t - (first_i - i) * step
    for (ia, ta), (ib, tb) in zip(known, known[1:]):
        for i in range(ia, ib + 1):
            out[i] = ta + (tb - ta) * (i - ia) / (ib - ia)
    last_i, last_t = known[-1]
    for i in range(last_i, n):
        out[i] = last_t + (i - last_i) * step
    return out


# The TV-Hub's own serial log export (IPACU.serial.log): a KEY=VALUE config block, which holds
# the owner's registration details and is skipped; a "LIVE DATA" status snapshot; then the
# serial stream with each line prefixed by a UTC stamp such as "Sep 26 2026 04:25:30.173".
_HUB_LIVE_MARK = "******** LIVE DATA ********"
_RE_HUB_TS = re.compile(r"^([A-Z][a-z]{2}) +(\d{1,2}) (\d{4}) (\d\d):(\d\d):(\d\d)\.(\d{3,4})(?: (.*))?$")
_MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


def _hub_time(m: re.Match) -> float | None:
    try:
        base = datetime(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)), int(m.group(4)),
                        int(m.group(5)), int(m.group(6)), tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return None
    return base.timestamp() + int(m.group(7)) / 1000  # milliseconds; the hub sometimes prints 1000


def _read_hub_export(lines: list[str]) -> list[list]:
    marked = any(line.startswith(_HUB_LIVE_MARK) for line in lines)
    section = "config" if marked else "serial"
    snapshot, serial, start = [], [], None
    for line in lines:
        if section == "config":
            if line.startswith(_HUB_LIVE_MARK):
                section = "live"
            continue
        if section == "live":
            if line and set(line) == {"*"}:
                section = "serial"
            elif line.startswith("LOG START:"):
                start = _parse_iso(line.split(":", 1)[1].strip())
            elif line:
                snapshot.append(line)
            continue
        m = _RE_HUB_TS.match(line)
        if m:
            if m.group(8) and m.group(8).strip():
                serial.append([_hub_time(m), "rx", m.group(8).rstrip()])
        elif line:
            serial.append([None, "rx", line])
    if start is None:
        start = next((e[0] for e in serial if e[0] is not None), None)
    return [[start, "hub", line] for line in snapshot] + serial


def read_log(path: str) -> tuple[list[tuple[float, str, str]], str]:
    """Load a capture. Returns ([(t, src, text), ...], timing) where timing says where
    the timestamps came from: 'recorded' (bridge log), 'hub' (the TV-Hub's own serial log
    export), 'gps' (interpolated between $GPRMC fixes) or 'estimated' (no GPS in the file:
    POS cadence, ending at the file's mtime)."""
    with open(path, "rb") as fh:
        text = fh.read().decode("utf-8", errors="replace")
    lines = [_RE_CTRL.sub("", line).rstrip() for line in text.splitlines()]
    stamped = sum(1 for line in lines[:400] if _RE_HUB_TS.match(line))
    if any(line.startswith(_HUB_LIVE_MARK) for line in lines[:1000]) or stamped > 0.5 * min(len(lines), 400):
        entries = _read_hub_export(lines)
        times = fill_times([e[0] for e in entries], end_hint=os.path.getmtime(path))
        return [(times[i], e[1], e[2]) for i, e in enumerate(entries)], "hub"
    entries: list[list] = []
    for line in lines:
        if not line:
            continue
        m = _RE_TS_LINE.match(line)
        if m:
            entries.append([_parse_iso(m.group(1)), _TAG_TO_SRC[m.group(2)], m.group(3)])
        else:
            entries.append([None, "rx", line])
    if not entries:
        return [], "recorded"

    recorded = sum(1 for e in entries if e[0] is not None)
    times: list = [e[0] for e in entries]
    timing = "recorded"
    if recorded < len(entries):
        n_pos = 0
        for i, e in enumerate(entries):
            if e[1] != "rx":
                continue
            s = e[2].lstrip("+")
            if s.startswith("POS:"):
                n_pos += 1
            if times[i] is None and s.startswith("$") and "RMC," in s[:8]:
                rec = _parse_nmea(s)
                if rec.get("cs_ok") and rec.get("utc"):
                    times[i] = rec["utc"]
        has_anchor = any(t is not None for t in times)
        timing = "recorded" if recorded else ("gps" if has_anchor else "estimated")
        step = POS_INTERVAL_S * n_pos / len(entries) if n_pos else 1.0
        times = fill_times(times, default_step=step, end_hint=os.path.getmtime(path))
    return [(times[i], e[1], e[2]) for i, e in enumerate(entries)], timing


def load_records(path: str) -> tuple[list[dict], str]:
    entries, timing = read_log(path)
    return [make_record(text, t, src, seq) for seq, (t, src, text) in enumerate(entries)], timing


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_EVENT_KINDS = {
    "mode", "search_target", "search_start", "sat_found", "transition", "boresight", "sleep_limits",
    "note", "azel_dist", "rf_normalize", "rf_freq", "rf_satconfig", "rf_satinstall", "rf_select",
    "rf_id", "rf_lnb", "echo", "tx", "local_echo", "reply", "sat_sel", "satinstall", "unknown_cmd",
    "version", "bridge", "power_test", "limit_switch", "siglevel", "tgt_location", "malformed", "ee_locked",
    "manual_ack", "jog_ack", "needs_mode",
}


def _fmt_t(t: float) -> str:
    return datetime.fromtimestamp(t).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Parse a TV-Hub port-50001 capture.")
    ap.add_argument("log", help="ncat -o capture, tvhub_server.py log, or the TV-Hub's own serial log export")
    ap.add_argument("--events", action="store_true", help="print the event timeline")
    ap.add_argument("--json", action="store_true", help="print every record as JSON lines")
    ap.add_argument("--csv", metavar="FILE", help="write +POS ticks (with the following +BST) as CSV")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):  # captures can hold serial noise a cp1252 console can't print
        sys.stdout.reconfigure(errors="replace")

    records, timing = load_records(args.log)
    if args.json:
        for r in records:
            print(json.dumps(r, separators=(",", ":")))
        return 0
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["time", "az", "el", "skew", "rf", "snr", "sat", "substate",
                        "boresight_az", "boresight_el", "boresight_tilt"])
            row = None
            for r in records:
                if r["kind"] == "pos":
                    if row:
                        w.writerow(row)
                    row = [datetime.fromtimestamp(r["t"]).astimezone().isoformat(timespec="milliseconds"),
                           r["az"], r["el"], r["skew"], r["rf"], r["snr"], r["sat"], r["substate"], "", "", ""]
                elif r["kind"] == "bst" and row and row[8] == "":
                    row[8:11] = [r["az"], r["el"], r.get("tilt", "")]
            if row:
                w.writerow(row)
        print(f"wrote {args.csv}")
        return 0
    if args.events:
        for r in records:
            if r["kind"] in _EVENT_KINDS or (r["kind"] == "state" and not r.get("reply")):
                print(f"{_fmt_t(r['t'])}  {r['kind']:<14} {r['raw']}")
        return 0

    if not records:
        print("no lines")
        return 0
    kinds = Counter(r["kind"] for r in records)
    unparsed = Counter(r["raw"] for r in records if r["kind"] == "unparsed")
    print(f"{args.log}: {len(records)} lines, {_fmt_t(records[0]['t'])} -> {_fmt_t(records[-1]['t'])}"
          f" ({timing} timestamps)")
    for kind, count in kinds.most_common():
        print(f"  {kind:<18}{count:>7}")
    if unparsed:
        print(f"\nunparsed lines ({sum(unparsed.values())}):")
        for raw, count in unparsed.most_common():
            print(f"  {count:>5}x  {raw}")
    bad_cs = [r for r in records if r["kind"] in ("gprmc", "nmea") and r["cs_ok"] is False]
    if bad_cs:
        print(f"\nNMEA checksum failures: {len(bad_cs)}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
