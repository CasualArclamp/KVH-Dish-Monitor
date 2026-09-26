# KVH Dish Monitor

A live dashboard for a KVH TracVision TV6 satellite antenna, built on the TV-Hub's
maintenance port (TCP 50001). It includes a parser for the antenna's telemetry, a small
bridge that talks to the TV-Hub, and a browser GUI with strip charts, events, transponder
config and health.

It needs only Python 3, with no packages to install (tested on 3.12 and 3.14). The GUI is
one HTML file with no external dependencies, so it works offline.

Unofficial: not affiliated with or endorsed by KVH Industries.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshot-dark.png">
  <img alt="The dashboard replaying samples/pol-switch.log. Tiles show SNR, RF, AGC, state, satellite, pointing, LNB supply and temperature. Strip charts show SNR falling to zero after a switch to vertical polarization and recovering after switching back to horizontal, above substate and state bands, next to the event log." src="docs/screenshot-light.png">
</picture>

*Replaying `samples/pol-switch.log` (times in UTC). Switching IS-19 to vertical loses the
signal, and switching back to horizontal re-acquires it.*

## Run it

```
python tvhub_server.py                                  # live: connects to the dish, opens http://127.0.0.1:8650/
python tvhub_server.py --read-only                      # live, never sends anything
python tvhub_server.py --replay samples/pol-switch.log  # look at a saved capture (add --speed 10 to watch it play)
```

Type the dish's TV-Hub address in the **Dish IP** box at the top of the page and press
Connect. A new address starts a fresh session (the old dish's history and satellites are
dropped) and is saved to `tvhub_config.json`, so the next launch reconnects there.
`--host` on the command line overrides the saved address for one run. The default is
192.168.50.214.

Live sessions are logged to `logs/tvhub-YYYYMMDD-HHMMSS.log` with arrival timestamps.
Those logs, and plain `ncat -o` captures, both work with `--replay`. For an ncat capture,
the timestamps are interpolated from the `$GPRMC` fixes.

## What the page shows

- **Tiles:** SNR and demod lock, RF metric, AGC, state/substate, satellite and
  transponder, pointing, LNB supply voltage (13 V = V, 18 V = H) with a check against the
  commanded polarization, and antenna temperature/status.
- **Strip charts:** SNR, RF, AGC, elevation and azimuth (POS and BST), plus optional skew
  and "BST third value". Under them is a substate band (green tracking, amber acquiring,
  orange searching, red lost) and a state band. Hairlines mark pol/band changes, lock and
  unlock, search modes, found, lost, wake and restarts. Reference lines show the sleep
  wake limits, the search threshold, the expected elevation and the highest GEO elevation
  for your latitude. Drag to zoom, double-click to reset, hover or use the arrow keys for
  values. **Table** lists the ticks and **CSV** downloads them.
- **Events:** search, tracking, state and lock changes, RF board messages, commands and
  replies, and bridge notes.
- **Cards:**
  - the current transponder (`RF: FREQ`)
  - the SATCONFIG slots, comparing the RF board's copy with the antenna's
  - the installed satellites, with their longitude and skew offset and the antenna's look
    angles next to ones computed from the GPS fix
  - search and sleep parameters, supply rails, boot self-tests, versions and GPS
- **Raw stream:** every line with the parser's classification. Unparsed lines are
  highlighted so new line types stand out.

## Commands (allowlist)

The TV-Hub's maintenance port is unauthenticated: anything on the LAN can send it
commands, including ones that restart or erase the antenna. Keep it off the internet.

The bridge enforces the allowlist itself; the GUI buttons are only a convenience. It
sends only:

- the bare read-only queries the TV-Hub sends at boot: `STATE SAT SATINSTALL VERSION GPS
  HOURS STATUS ANTLNB SIDELOBE SLEEP SEARCHTIMEOUT HW @VER @FPGAVER =SERNUM`
- `SAT,<sat>,<H|V>,<L|H>`, and only for a satellite already seen in this session's
  telemetry. The GUI asks you to confirm these.

It refuses everything else (`ZAP`, `HALT`, `CLEAREE`, `=CAL…`, `@SAVE`, …) and logs the
refusal. The limit is two commands per second, and it never sends anything on a timer.
The web server listens on 127.0.0.1 only and rejects cross-site requests.

## Sample captures

Both are real captures from a TV6. The GPS position has been moved to a dummy point and
the serial number blanked.

- `samples/pol-switch.log`: tracking IS-19 (166°E). Switching to vertical loses the
  signal, and switching back to horizontal re-acquires it.
- `samples/install-restart.log`: a satellite install (SATSETUP/SATCONFIG), a `ZAP`
  restart with the boot self-tests, and a search that finds USER6I.

## Parser CLI

```
python tvhub_parser.py samples/pol-switch.log                  # line counts by kind + any unparsed lines
python tvhub_parser.py samples/pol-switch.log --events         # event timeline
python tvhub_parser.py samples/pol-switch.log --csv ticks.csv  # +POS/+BST ticks as CSV
python tvhub_parser.py samples/pol-switch.log --json           # every record as JSON lines
```

## Tests

```
python -m unittest discover -s tests
```

The server tests use a fake TV-Hub on localhost. They check that only allowlisted bytes
ever reach the socket and that nothing goes to the old dish after an address change.
