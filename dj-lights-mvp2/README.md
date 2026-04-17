# dj-lights MVP2

Live DJ lighting driven by Pioneer Pro DJ Link. Single Python process listens
to the XDJ-XZ, fetches Rekordbox PSSI phrase data for each loaded track, maps
phrases to scene modes, and drives DMX fixtures + Govee bulbs as beats advance.

```
XDJ track loaded
  -> fetch Rekordbox PSSI phrases over NFS (via vcdj peer)
  -> map phrases to modes (intro/groove/buildup/breakdown/drop/outro)
XDJ beat updates (CDJ-Status packets on port 50002)
  -> pick active deck (hysteresis on crossfade)
  -> section_for_beat() -> current mode
  -> direct_lights.apply_mode(mode)
       -> DMX render thread
       -> Govee LAN UDP (fast) or cloud fallback
```

## Files

- `main.py` — Pro DJ Link client + state machine. Runs forever.
  - Active-deck hysteresis: when both decks play during a crossfade, we stay
    on the outgoing deck until it stops, *then* hand off. Prevents lights
    from flapping between two tracks' modes mid-mix.
  - Dual-deck preload: PSSI analysis is fetched for **any** deck that loads
    a new track, not just the active one. When a crossfade promotes the
    other deck, mode lookup is instantaneous instead of stuck on the
    previous track's last mode.
  - Tagged prints (`[deck]`, `[track]`, `[phrases]`, `[mode]`, `[vcdj]`,
    `[dmx]`, `[govee]`) are what the dashboard tails.
- `analysis.py` — PSSI → mode mapping (by mood: low/mid/high).
- `bridge.py` — per-track analysis cache in `cache/`.
- `direct_lights.py` — mode → render thread → DMX + Govee.
- `govee_lan.py` — in-process Govee client, LAN UDP (preferred), cloud
  fallback. Error messages live under `msg` (control endpoint) or `message`
  (device list) — both are read.
- `dashboard.py` — sidecar HTTP server. Tails `logs/main.log`, exposes
  `/api/state` (JSON) and `/` (HTML) on port 8787. Survives main.py
  restarts (inode-aware log tailing).

## Pro DJ Link — what makes the XDJ talk to us

**Keep it working.** See [HANDOFF.md](../HANDOFF.md) for the full story; the
load-bearing rules are:

1. **Announce as rekordbox**, `player_number = 17` (0x11),
   `device_type=rekordbox`, `is_rekordbox=True`. XDJ-XZ drops CDJ-class
   announcements at pn=5.
2. **Send DJM-class status (type 0x29)** every 200ms with `u2=1`,
   `remaining_bytes=0x14`, unicast from source port 50002 to each peer IP.
3. **Reply to every `rekordbox_hello` (0x10) with a `rekordbox_reply` (0x11)**
   — the XDJ pings us every ~5s. Silent drop → XDJ de-trusts us at ~60s and
   stops unicasting CDJ-Status. Handled automatically in
   `prodj/core/prodj.py::handle_status_packet` →
   `prodj/core/vcdj.py::send_rekordbox_reply`. **Log line to confirm it's
   wired:** `Sent first rekordbox_reply to 169.254.x.x (keeps XDJ
   unicasting status)`.
4. **Pin the send socket to the right NIC**. macOS can have two
   `169.254.0.0/16` routes (en0 + en8); `vcdj.set_interface_data` uses
   `IP_BOUND_IF = 25` to force packets out en8.

## Running

From the repo root, with the venv:

```bash
./.venv/bin/python dj-lights-mvp2/main.py
```

Dashboard (optional, runs independently; start it before or after main.py):

```bash
./.venv/bin/python dj-lights-mvp2/dashboard.py
# -> http://localhost:8787
```

Prerequisites (see [`../SETUP.md`](../SETUP.md) for details):

- XDJ-XZ connected to Mac over ethernet on the link-local subnet
  (`169.254/16`). Interface is `en8` by default — confirm with `ifconfig en8`.
- Enttec Open DMX USB plugged in (`/opt/homebrew/lib/libftdi1.dylib`
  installed). Device path autodiscovered via `/dev/cu.usbserial-*`.
- `~/.config/govee/credentials.json` with API key.
- Govee devices with LAN Control enabled. Run `govee refresh` while on the
  same WiFi to cache LAN IPs in `~/.config/govee/devices.json`.
- Rekordbox export on the XDJ's SD or USB slot (for PSSI phrase data).

## Health check — what a working session looks like

The first ~10 seconds of `logs/main.log` should show, in order:

1. `Listening on 0.0.0.0:5000[012]` — prodj sockets bound.
2. `VCDJ bound send sock to iface en8 (idx=N)` — `IP_BOUND_IF` pinned.
3. `New Player 1: XDJ-XZ, 169.254.x.x` — XDJ discovered via keepalive.
4. `All-in-one unit detected at 169.254.x.x: created deck 2` — both decks.
5. `STATUS PACKET deck 1: beat_count None -> N` — inbound status unlocked.
6. `Sent first rekordbox_reply to 169.254.x.x` — handshake acknowledged.
7. `[track] deck=1 id=N "Title" — K phrases` — PSSI loaded.
8. `[mode] beat=N -> drop` — first mode dispatched; `[dmx] opened (...)`
   and lights follow.

If status packets stop flowing after ~60s while vcdj ticks continue
outbound, the `rekordbox_reply` path has regressed — grep for
`rekordbox_hello` in the log; if every one says `"ignoring"`, the
interceptor in `prodj.py` isn't firing.

## Speed notes

- Govee LAN UDP: ~10ms, no rate limit. Used whenever a device has a cached
  LAN IP in `~/.config/govee/devices.json`.
- Govee cloud: 100-500ms per request, rate limited. Only used for devices
  that haven't been discovered over LAN yet.
- DMX: libftdi direct, ~1ms per 512-byte frame.
- Mode change → render thread swap: sub-millisecond.
- First Govee scan happens at startup via `direct_lights.warm_up()`;
  subsequent scene changes hit the light with zero discovery overhead.

## Shutdown

`Ctrl-C` or `SIGTERM`. The shutdown path runs `direct_lights.blackout()`
(clears DMX + turns Govee off) then stops vcdj cleanly. If main.py hangs,
`pkill -9 -f dj-lights-mvp2/main.py` is safe — vcdj sockets auto-close on
process exit.
