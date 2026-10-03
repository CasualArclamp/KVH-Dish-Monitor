"""Client for the TV-Hub's XML web service (POST /webservice.php on port 80).

The TV-Hub's own web pages talk to it with
    <ipacu_request><message name="antenna_status" /></ipacu_request>
and poll antenna_status about once a second. This module sends only the messages in
READ_ONLY, with only the parameters listed for each, plus one change the user asks for by
clicking: select_satellite with install=N, which switches to another satellite of the
installed group (what the hub's own "installed group" view sends). Everything else is
refused before a request is built: every set_* message, select_satellite with install=Y (a
reinstall), reboot, reset_software, install_software, clear_event_history (a "get" that
wipes the hub's alert log), and the messages that return the owner's registration or
network settings. Stdlib only.

The parse_* functions turn replies into small JSON-able dicts for the monitor. They keep
only what it shows: no GPS position, serial numbers or modem address.
"""

from __future__ import annotations

import calendar
import math
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

WEB_PORT = 80
PATH = "/webservice.php"

# message name -> parameters it may carry
READ_ONLY = {
    "antenna_status": (),
    "power": (),
    "antenna_versions": (),
    "get_antenna_config": (),
    "ophours": (),
    "get_satellite_list": (),
    "get_satellite_params": ("antSatID",),
    "get_lnb_list": (),
    "get_gps": (),
    "get_heading_config": (),
    "get_blockage_zones": (),
    "get_hazard_zones": (),
    "get_event_history_count": (),
    "get_recent_event_history": ("begin_at_event", "how_many_events"),
    "get_autoswitch_status": (),
}
# The only message that changes anything, sent only through select_satellite() below.
SELECT = "select_satellite"
_SELECT_PARAMS = ("antSatID", "install")
_PARAM_RULES = {
    "install": re.compile(r"^N$"),  # N: switch within the installed group; Y (reinstall) is never sent
    "antSatID": re.compile(r"^[A-Za-z0-9]{1,12}$"),
    "begin_at_event": re.compile(r"^[1-9]\d{0,3}$"),
    "how_many_events": re.compile(r"^[1-9]\d{0,3}$"),
}

# From the hub web UI's WebService.js
ERRORS = {
    "-1": "not written yet", "1": "error", "2": "invalid XML", "3": "unknown message", "4": "missing element",
    "5": "invalid element value", "6": "missing data", "7": "unknown file name", "8": "file not found",
    "9": "file unreadable", "10": "file locked", "11": "file unwritable", "12": "event not found",
    "13": "too many events", "14": "datastore unavailable", "15": "timeout", "16": "invalid update file",
    "17": "duplicate data", "18": "configuration error (LNB not configured)",
}


class WebServiceError(Exception):
    code: str | None = None  # the hub's error code, when it answered with one


class HubUnreachable(WebServiceError):
    """No usable HTTP answer (refused, timed out, HTTP error status), as opposed to the hub
    answering with one of its own error codes."""


# Codes after which asking again is pointless: the hub does not know or accept the request.
PERMANENT_ERRORS = {"2", "3", "4", "5"}


def build_request(name: str, params: dict | None = None, allow_select: bool = False) -> bytes:
    """The request body for an allowlisted read-only message (or, with allow_select, the
    group switch), else WebServiceError."""
    if name in READ_ONLY:
        allowed = READ_ONLY[name]
    elif name == SELECT and allow_select:
        allowed = _SELECT_PARAMS
        if set(params or {}) != set(_SELECT_PARAMS):
            raise WebServiceError(f"{name} needs exactly {', '.join(_SELECT_PARAMS)}")
    else:
        raise WebServiceError(f"{name!r} is not an allowlisted message")
    parts = []
    for key, value in (params or {}).items():
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise WebServiceError(f"parameter {key}={value!r} not allowed for {name}")
        value = str(value)
        if key not in allowed or not _PARAM_RULES[key].match(value):
            raise WebServiceError(f"parameter {key}={value!r} not allowed for {name}")
        parts.append(f"<{key}>{value}</{key}>")
    return f'<ipacu_request><message name="{name}" />{"".join(parts)}</ipacu_request>'.encode("ascii")


def _url(host: str, port: int) -> str:
    return f"http://{'[' + host + ']' if ':' in host else host}:{port}{PATH}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # the hub never redirects; don't follow anyone elsewhere
        return None


# Straight to the hub: no proxy from the environment, no redirects.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def call(host: str, name: str, params: dict | None = None, timeout: float = 5.0, port: int = WEB_PORT,
         allow_select: bool = False) -> ET.Element:
    """Send one allowlisted message and return the <ipacu_response> element."""
    body = build_request(name, params, allow_select)
    req = urllib.request.Request(_url(host, port), data=body, method="POST", headers={"Content-Type": "text/xml"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            data = resp.read(2_000_000)
    except urllib.error.HTTPError as e:  # before URLError, which it subclasses
        raise HubUnreachable(f"{name}: HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise HubUnreachable(f"{name}: {getattr(e, 'reason', None) or e}") from e
    return check_reply(name, data)


def _unknown_message(name: str) -> WebServiceError:
    """The hub doesn't implement this message; raise it as code 3 so callers stop asking."""
    err = WebServiceError(f"{name}: not supported by this TV-Hub (unknown message)")
    err.code = "3"  # the hub's own "unknown message" code, already in PERMANENT_ERRORS
    return err


def check_reply(name: str, data: bytes) -> ET.Element:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        # Firmware that doesn't implement a message answers with malformed XML whose body names
        # the generic "unknown_xml_message" handler. Treat that as a permanent "unknown message"
        # rather than a transient parse error, so the poller stops asking for it this session.
        if b"unknown_xml_message" in data:
            raise _unknown_message(name) from e
        raise WebServiceError(f"{name}: reply is not XML ({e})") from e
    msg = root.find("message")
    if msg is not None and msg.get("name") == "unknown_xml_message":  # same, but well-formed
        raise _unknown_message(name)
    code = msg.get("error") if msg is not None else None
    if code != "0":
        err = WebServiceError(f"{name}: hub answered error {code} ({ERRORS.get(code or '', 'unknown')})")
        err.code = code
        raise err
    return root


# ---------------------------------------------------------------------------
# Replies -> dicts
# ---------------------------------------------------------------------------

def _text(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    v = el.findtext(path)
    return v.strip() if v is not None and v.strip() != "" else None


def _num(el: ET.Element | None, path: str) -> float | None:
    v = _text(el, path)
    try:
        x = float(v) if v is not None else None
    except ValueError:
        return None
    return x if x is not None and math.isfinite(x) else None  # "nan"/"inf" would make invalid JSON for the page


def parse_antenna_status(root: ET.Element) -> dict:
    acu, ant, sat, gps = root.find("acu"), root.find("antenna"), root.find("satellite"), root.find("gps")
    leds = {}
    for kind in ("power", "acu", "antenna"):
        led = acu.find("led_" + kind) if acu is not None else None
        if led is not None:
            leds[kind] = {"color": _text(led, "color"), "state": _text(led, "state"), "message": _text(led, "message")}
    return {
        "hub_state": _text(acu, "state"), "line1": _text(acu, "line1"), "line2": _text(acu, "line2"), "leds": leds,
        "state": _text(ant, "state"), "snr": _num(ant, "rf/snr"), "bars": _num(ant, "rf/bars"),
        "heading": _num(ant, "brst/hdg"), "az_bow": _num(ant, "brst/az_bow"),
        "bst_az": _num(ant, "brst/az"), "bst_el": _num(ant, "brst/el"), "bst_tilt": _num(ant, "brst/tilt"),
        "motor_az": _num(ant, "motor/az"), "motor_el": _num(ant, "motor/el"), "motor_skew": _num(ant, "motor/skew"),
        "sat": _text(sat, "antSatID"), "sat_name": _text(sat, "name"), "sat_lon": _num(sat, "lon"),
        "selected": _text(sat, "selected"),
        "gps_state": _text(gps, "state"), "gps_source": _text(gps, "source"),  # the position itself is left out
    }


def parse_power(root: ET.Element) -> dict:
    acu, au = root.find("acu"), root.find("au")
    return {
        "hub": {k: _num(acu, k) for k in ("inputsupplyv", "input42v", "input24v", "output42v", "output24v",
                                          "eight", "five", "three_three", "temp_celsius")},
        "antenna": {k: _num(au, k) for k in ("dc", "motor", "eight", "five", "lnb", "temp_celsius")},
    }


def parse_versions(root: ET.Element) -> dict:
    ver = lambda path: _text(root, path)  # noqa: E731
    lnb = root.find("lnb")
    return {
        "hub_software": ver("acu/ver"), "in_sync": ver("ver_sync"), "sat_library": ver("sat_list/ver"),
        "model": ver("au/model"), "system_id_model": ver("au/systemIDModel"), "antenna_rev": ver("au/rev"),
        "antenna_software": ver("au/ver"), "rf": ver("rf/ver"), "az_el": ver("az_el/ver"), "skew": ver("skew_xaz/ver"),
        "diseqc": ver("diseqc/ver"), "ipautosw": ver("ipautosw/ver"), "fpga": ver("fpga/ver"),
        "lnb": {k: _text(lnb, k) for k in ("part", "name", "polarization", "voltage", "LO1_freq", "LO1_convert",
                                          "LO1_tone", "LO2_freq", "LO2_convert", "LO2_tone")} if lnb is not None else None,
    }


def parse_config(root: ET.Element) -> dict:
    return {c.tag: (c.text or "").strip() for c in root if c.tag != "message"}


def parse_ophours(root: ET.Element) -> dict:
    return {"hours": _num(root, "hours")}


def parse_satellite_list(root: ET.Element) -> dict:
    sats = []
    for s in root.iter("satellite"):
        sats.append({"id": _text(s, "antSatID"), "name": _text(s, "name"), "lon": _num(s, "lon"),
                     "region": _text(s, "region"), "favorite": _text(s, "favorite") == "TRUE",
                     "user": (_text(s, "antSatID") or "").startswith("USER")})
    return {"sats": sats}


def parse_satellite_params(root: ET.Element) -> dict:
    xps = []
    for x in root.iter("xponder"):
        xps.append({k: _text(x, k) for k in ("id", "display", "pol", "band", "freq", "symRate", "fec", "netID", "modType")})
    return {
        "sat": _text(root, "antSatID"), "name": _text(root, "name"), "region": _text(root, "region"),
        "lon": _num(root, "lon"), "skew_offset": _num(root, "skew"), "computed_skew": _num(root, "computedSkew"),
        "lo1": _text(root, "lo1"), "lo2": _text(root, "lo2"), "kumode": _text(root, "kumode"),
        "preferred_polarity": _text(root, "preferredPolarity"), "enable": _text(root, "enable"),
        "xponders": sorted(xps, key=lambda x: int(x["id"]) if (x["id"] or "").isdigit() else 99),
    }


def parse_lnb_list(root: ET.Element) -> dict:
    """The LNB presets the hub offers, and whether custom-LNB entry is enabled (<enable>Y|N</enable>)."""
    group = root.find("lnb_list")
    names = [(n.text or "").strip() for n in group.iter("name")] if group is not None else []
    return {"lnbs": [n for n in names if n], "custom_enabled": _text(root, "enable") == "Y"}


def parse_gps(root: ET.Element) -> dict:
    """The hub's GPS fix state only (ACQUIRED | ACQUIRING | ERROR | MANUAL). The position itself
    (lat/lon/city) is deliberately dropped, as everywhere else in this module; the monitor already
    shows the site from the antenna's $GPRMC, and screenshots of the page get committed."""
    return {"state": _text(root, "state")}


def parse_heading_config(root: ET.Element) -> dict:
    """Whether a true-heading input (an NMEA gyro or compass) is configured and which source, if any,
    is selected and active. With no heading the dome's azimuth offset has to be learned while tracking."""
    sources = []
    for bus in ("nmea0183", "nmea2000"):
        b = root.find(bus)
        if b is None:
            continue
        bus_enabled = _text(b, "enable") == "Y"
        for m in b.iter("nmea_message"):
            sources.append({
                "bus": bus, "bus_enabled": bus_enabled, "name": _text(m, "nmea_name"),
                "source": _text(m, "nmea_source"), "heading": _num(m, "heading_value"),
                "state": _text(m, "state"), "selected": _text(m, "selected") == "Y",
            })
    active = next((s for s in sources if s["selected"] and (s["state"] or "").upper() == "ACTIVE" and s["bus_enabled"]), None)
    return {"sources": sources, "selected": active, "has_heading": active is not None}


def _zones(el: ET.Element | None) -> list:
    """The AZ/EL keep-out rectangles in a <zone_list>/<acu_list>/<ant_list>."""
    if el is None:
        return []
    out = []
    for z in el.iter("zone"):
        out.append({"id": _text(z, "id"), "state": _text(z, "state"),
                    "az_min": _num(z, "az_min"), "az_max": _num(z, "az_max"),
                    "el_min": _num(z, "el_min"), "el_max": _num(z, "el_max")})
    return out


def parse_blockage_zones(root: ET.Element) -> dict:
    """Azimuth/elevation sectors where the dish's view is known to be obstructed."""
    zones = _zones(root.find("zone_list"))
    n = _num(root, "total_zones")
    return {"el_support": _text(root, "el_support") == "TRUE",
            "total": int(n) if n is not None else len(zones), "zones": zones}


def parse_hazard_zones(root: ET.Element) -> dict:
    """RF-transmit keep-out sectors. The hub holds its own copy (acu_list) and the antenna's
    (ant_list), each with an override flag; <mismatch> says whether the two copies disagree."""
    def side(tag: str) -> dict | None:
        el = root.find(tag)
        return None if el is None else {"override": _text(el, "override") == "ON", "zones": _zones(el)}
    return {"el_support": _text(root, "el_support") == "TRUE", "mismatch": _text(root, "mismatch") == "YES",
            "acu": side("acu_list"), "ant": side("ant_list")}


def parse_event_count(root: ET.Element) -> dict:
    n = _num(root, "event_count")
    return {"count": int(n) if n is not None else None}


_RE_EVENT = re.compile(r"^(?P<date>[A-Z][a-z]{2} +\d{1,2} +\d{4} +\d\d:\d\d:\d\d)\s+(?P<level>.*?)::\s*(?P<text>.*)$", re.S)
_MONTHS = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


def parse_event_line(line: str) -> dict:
    """'Sep 26 2026 10:05:31    ALERT::  Polarization and ...' -> t (the hub logs UTC), level, text."""
    m = _RE_EVENT.match(line.strip())
    if not m:
        return {"t": None, "level": None, "text": line.strip()}
    mon, day, year, hms = m.group("date").split()
    hh, mm, ss = (int(x) for x in hms.split(":"))
    try:
        t = calendar.timegm((int(year), _MONTHS[mon], int(day), hh, mm, ss, 0, 0, 0))
    except KeyError:
        t = None
    return {"t": t, "level": m.group("level").strip(), "text": m.group("text").strip()}


def parse_events(root: ET.Element) -> dict:
    return {"events": [parse_event_line(e.text or "") for e in root.iter("event") if (e.text or "").strip()]}


def parse_autoswitch(root: ET.Element) -> dict:
    """The installed group and whether autoswitch (receivers choose the satellite) is on.
    Receiver serial numbers and addresses are left out."""
    master = root.find("master")
    sats = []
    group = root.find("satellites")
    for slot in ("A", "B", "C", "D"):
        el = group.find(slot) if group is not None else None
        if el is not None and _text(el, "antSatID"):
            sats.append({"slot": slot, "id": _text(el, "antSatID"), "name": _text(el, "name"), "lon": _num(el, "lon")})
    return {
        "available": _text(root, "available") == "Y", "enabled": _text(root, "enable") == "Y",
        "service": _text(root, "service"), "group": _text(root, "satellite_group"),
        "master": {"name": _text(master, "name"), "slot": _text(master, "sat"), "valid": _text(master, "valid")},
        "sats": sats,
    }


PARSERS = {
    "antenna_status": parse_antenna_status, "power": parse_power, "antenna_versions": parse_versions,
    "get_antenna_config": parse_config, "ophours": parse_ophours, "get_satellite_list": parse_satellite_list,
    "get_satellite_params": parse_satellite_params, "get_lnb_list": parse_lnb_list,
    "get_gps": parse_gps, "get_heading_config": parse_heading_config,
    "get_blockage_zones": parse_blockage_zones, "get_hazard_zones": parse_hazard_zones,
    "get_event_history_count": parse_event_count,
    "get_recent_event_history": parse_events, "get_autoswitch_status": parse_autoswitch,
}


def select_satellite(host: str, sat: str, timeout: float = 8.0, port: int = WEB_PORT) -> None:
    """Switch to another satellite of the installed group (install=N: no reinstall)."""
    call(host, SELECT, {"antSatID": sat, "install": "N"}, timeout, port, allow_select=True)


def fetch(host: str, name: str, params: dict | None = None, timeout: float = 5.0, port: int = WEB_PORT) -> dict:
    return PARSERS[name](call(host, name, params, timeout, port))


if __name__ == "__main__":  # quick look: python tvhub_webservice.py 192.168.50.214
    import json
    import sys
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.50.214"
    for name in ("antenna_status", "power", "antenna_versions", "get_antenna_config", "ophours"):
        print(name, json.dumps(fetch(host, name), indent=1))
        time.sleep(1)
